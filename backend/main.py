"""RentMate backend entrypoint (thin wiring only).

Business logic lives in the domain modules; this file only creates the
app and mounts routers. Do not add business rules here.

The app is built by `create_app(db_path, storage_root)` so tests (and
prototypes) can point the backend at a temp database and a temp
storage directory. `uvicorn main:app` uses the module-level `app`,
which reads its defaults from the environment:

    DATABASE_PATH          (fallback: backend/rentmate.db)
    EVIDENCE_STORAGE_ROOT  (fallback: backend/evidence_storage)
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from fastapi import APIRouter, FastAPI

load_dotenv(Path(__file__).with_name(".env"))

from evidence.router import build_evidence_service, router as evidence_router
from maintenance.api import router as maintenance_router
from review.api import router as review_router
from reporting.api import router as reporting_router

_DEFAULT_DB_PATH = os.path.join(os.path.dirname(__file__), "rentmate.db")
_DEFAULT_STORAGE_ROOT = os.path.join(os.path.dirname(__file__), "evidence_storage")


ops_router = APIRouter()

# ---------------------------------------------------------------------------
# UC5 scheduled job entry (rent due reminders / overdue alerts).
#
# The job logic lives in the lease_rent module (owner: Ziheng); this file
# only provides the trigger. Until an auto-scheduler is wired in, the
# endpoint is the manual trigger (ops / demo use). The lease_rent module
# must keep the run idempotent: a second run at the same `now` must not
# create duplicate alerts or drafts (dedup via stable event_id /
# source_id, see db/schema.sql).
# ---------------------------------------------------------------------------
@ops_router.post("/api/ops/rent-alerts/run")
def run_rent_alerts(now: str | None = None) -> dict:
    """Trigger the rent alert scan. `now` is an optional ISO-8601 UTC
    override (demo/test); omitted means current UTC time.

    Returns 503 until the lease_rent module implements
    `run_due_alerts` (owner: Ziheng)."""
    from datetime import datetime, timezone

    try:
        from lease_rent.service import run_due_alerts  # deferred import
    except ImportError:
        from fastapi import HTTPException

        raise HTTPException(
            status_code=503, detail="lease_rent.run_due_alerts not implemented yet"
        )

    now_dt = datetime.now(timezone.utc) if now is None else datetime.fromisoformat(now)
    return run_due_alerts(now=now_dt)


def create_app(db_path: str | None = None, storage_root: str | None = None) -> FastAPI:
    """Build the FastAPI app with all currently mounted routers.

    Mounting a module's router happens here when the module lands its
    first endpoints; the module's own wiring helper is called here too
    (kept in the module, not inlined, so main stays dependency-light).
    """
    app = FastAPI(title="RentMate API", version="0.1.0")

    db_path = db_path or os.environ.get("DATABASE_PATH", _DEFAULT_DB_PATH)
    storage_root = storage_root or os.environ.get("EVIDENCE_STORAGE_ROOT", _DEFAULT_STORAGE_ROOT)

    # Evidence module (SCRUM-8): property-scoped upload / list / get /
    # download. Identity via prototype headers — TEST-ONLY, not real
    # authentication (see evidence.router for the full warning).
    app.state.evidence_service = build_evidence_service(db_path, storage_root)
    app.include_router(evidence_router, prefix="/api")
    app.include_router(maintenance_router)
    app.include_router(review_router)
    app.include_router(reporting_router)
    app.include_router(ops_router)

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    return app


# Module-level app for `uvicorn main:app` (defaults from environment).
app = create_app()
