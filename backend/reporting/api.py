"""Trusted monthly scheduler HTTP entrypoint."""

from __future__ import annotations

import hmac
import os
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict

from contracts.reporting import (
    GenerationRequest,
    ReportGenerationError,
    ReportPeriod,
    ScheduledReportTrigger,
)
from lease_rent.reporting import SQLiteLeaseRentReportSource
from llm.reporting import LlmReportNarrator
from maintenance.reporting import SQLiteMaintenanceReportSource
from maintenance.store import Store
from reporting.collector import ReportDataCollector
from reporting.repository import ReportRepository
from reporting.service import ReportService
from shared.sqlite import init_connection

router = APIRouter(prefix="/api/ops/monthly-reports", tags=["Reporting Operations"])
bearer = HTTPBearer(auto_error=False)


class ScheduledRunBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    property_id: str
    year: int
    month: int


def get_connection(request: Request) -> Iterator[sqlite3.Connection]:
    path = request.app.state.db_path
    connection = init_connection(path)
    try:
        yield connection
    finally:
        connection.close()


def require_scheduler(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> None:
    configured = os.getenv("RENTMATE_SCHEDULER_TOKEN", "")
    if not configured:
        raise HTTPException(503, "Monthly scheduler token is not configured")
    if credentials is None or not hmac.compare_digest(
        credentials.credentials, configured
    ):
        raise HTTPException(401, "Invalid scheduler credentials")


def get_service(
    connection: Annotated[sqlite3.Connection, Depends(get_connection)],
) -> ReportService:
    database = Path(connection.execute("PRAGMA database_list").fetchone()[2])
    collector = ReportDataCollector(
        SQLiteMaintenanceReportSource(Store(database)),
        SQLiteLeaseRentReportSource(connection),
    )
    return ReportService(
        ReportRepository(connection), collector, LlmReportNarrator()
    )


@router.post("/run")
def run_monthly_report(
    body: ScheduledRunBody,
    _: Annotated[None, Depends(require_scheduler)],
    service: Annotated[ReportService, Depends(get_service)],
):
    try:
        period = ReportPeriod(year=body.year, month=body.month)
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    trigger = ScheduledReportTrigger.for_slot(body.property_id, period)
    result = service.generate(
        GenerationRequest(property_id=body.property_id, period=period, trigger=trigger),
        caller="system:scheduler",
    )
    if isinstance(result, ReportGenerationError):
        status = 404 if result.code == "authorization_denied" else 503
        raise HTTPException(status, result.model_dump(mode="json"))
    return result
