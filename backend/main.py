"""RentMate backend entrypoint (thin wiring only).

Business logic lives in the domain modules; this file only creates the
app and mounts routers. Do not add business rules here.

Config loading (order matters):
1. `load_dotenv` runs at module import time, BEFORE any business-module
   import and before the evidence service is built, so `backend/.env`
   provides defaults for:

       DATABASE_PATH          (fallback: backend/rentmate.db)
       EVIDENCE_STORAGE_ROOT  (fallback: backend/evidence_storage)

   Values already present in the process environment take precedence
   (the dotenv load does not override existing variables).

The app is built by `create_app(db_path, storage_root)` so tests (and
prototypes) can point the backend at a temp database and a temp
storage directory. `uvicorn main:app` uses the module-level `app`,
which is the single `create_app()` call at the bottom of this file.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse

# Load backend/.env before importing business modules that read env
# config (evidence wiring, llm adapter) and before the service is built.
load_dotenv(Path(__file__).with_name(".env"))

from communication.api import ops_router as communication_ops_router  # noqa: E402
from communication.api import router as communication_router  # noqa: E402
from evidence.router import build_evidence_service, router as evidence_router  # noqa: E402
from llm.classify import IDocumentClassifier  # noqa: E402
from maintenance.api import router as maintenance_router  # noqa: E402
from notification.api import ops_router as notification_ops_router  # noqa: E402
from notification.api import router as notification_router  # noqa: E402
from review.api import router as review_router  # noqa: E402
from reporting.api import require_scheduler, router as reporting_router  # noqa: E402
from reporting.manager_api import router as manager_reporting_router  # noqa: E402

_DEFAULT_DB_PATH = os.path.join(os.path.dirname(__file__), "rentmate.db")
_DEFAULT_STORAGE_ROOT = os.path.join(os.path.dirname(__file__), "evidence_storage")
_DEMO_PAGE = Path(__file__).resolve().parents[1] / "frontend" / "index.html"


ops_router = APIRouter()

# ---------------------------------------------------------------------------
# UC4/UC5 scheduled job entry (overdue rent reminders / lease deadlines).
#
# The job logic lives in the lease_rent module (owner: Ziheng); this file
# only provides the trigger. Until an auto-scheduler is wired in, the
# endpoint is the manual trigger (ops / demo use). The lease_rent module
# must keep the run idempotent: a second run at the same `now` must not
# create duplicate alerts or drafts (dedup via stable event_id /
# source_id, see db/schema.sql).
# ---------------------------------------------------------------------------
@ops_router.post(
    "/api/ops/rent-alerts/run",
    dependencies=[Depends(require_scheduler)],
)
def run_rent_alerts(request: Request, now: str | None = None) -> dict:
    """Trigger the rent alert scan. `now` is an optional ISO-8601 UTC
    override (demo/test); omitted means current UTC time.

    The same trusted scheduler credential used by monthly reporting is
    required; ordinary callers cannot impersonate the scheduler."""
    from datetime import datetime, timezone

    try:
        from lease_rent.service import run_due_alerts  # deferred import
    except ImportError:
        raise HTTPException(
            status_code=503, detail="lease_rent.run_due_alerts not implemented yet"
        )

    try:
        now_dt = (
            datetime.now(timezone.utc)
            if now is None
            else datetime.fromisoformat(now.replace("Z", "+00:00"))
        )
        return run_due_alerts(now=now_dt, db_path=request.app.state.db_path)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


def create_app(
    db_path: str | None = None,
    storage_root: str | None = None,
    *,
    classifier: IDocumentClassifier | None = None,
) -> FastAPI:
    """Build the FastAPI app with all currently mounted routers.

    Mounting a module's router happens here when the module lands its
    first endpoints; the module's own wiring helper is called here too
    (kept in the module, not inlined, so main stays dependency-light).
    """
    app = FastAPI(title="RentMate API", version="0.1.0")

    db_path = db_path or os.environ.get("DATABASE_PATH", _DEFAULT_DB_PATH)
    storage_root = storage_root or os.environ.get("EVIDENCE_STORAGE_ROOT", _DEFAULT_STORAGE_ROOT)
    app.state.db_path = db_path

    # Maintenance + review routers (prefixes live on the routers).
    app.include_router(maintenance_router)
    app.include_router(review_router)
    app.include_router(reporting_router)
    app.include_router(manager_reporting_router)
    app.include_router(ops_router)
    app.include_router(notification_router)
    app.include_router(notification_ops_router)
    app.include_router(communication_router)
    app.include_router(communication_ops_router)

    # Evidence uses server-side Bearer identities, tenant_property reads,
    # and manager_property authorization for independent manager reviews.
    app.state.evidence_service = build_evidence_service(db_path, storage_root, classifier=classifier)
    app.include_router(evidence_router, prefix="/api")

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.get("/demo", include_in_schema=False)
    def integrated_demo():
        return FileResponse(_DEMO_PAGE)

    return app


# Module-level app for `uvicorn main:app` (single instance for the app
# above; env defaults resolved by load_dotenv + process environment).
app = create_app()
