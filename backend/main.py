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

from fastapi import FastAPI

from evidence.router import build_evidence_service, router as evidence_router

_DEFAULT_DB_PATH = os.path.join(os.path.dirname(__file__), "rentmate.db")
_DEFAULT_STORAGE_ROOT = os.path.join(os.path.dirname(__file__), "evidence_storage")


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

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    return app


# Module-level app for `uvicorn main:app` (defaults from environment).
app = create_app()
