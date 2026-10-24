"""RentMate backend entrypoint (thin wiring only).

Business logic lives in the domain modules; this file only creates the
app and mounts routers. Do not add business rules here.
"""

from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI

load_dotenv(Path(__file__).with_name(".env"))

app = FastAPI(title="RentMate API", version="0.1.0")

from maintenance.api import router as maintenance_router
from review.api import router as review_router
from reporting.api import router as reporting_router

app.include_router(maintenance_router)
app.include_router(review_router)
app.include_router(reporting_router)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


# Routers are mounted as modules land their first endpoints, e.g.:
# from communication import router as communication_router
# app.include_router(communication_router, prefix="/api")


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
@app.post("/api/ops/rent-alerts/run")
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
