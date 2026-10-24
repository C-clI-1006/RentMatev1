"""Manager-initiated monthly report generation over HTTP.

The manager identity is resolved from the server-side auth context
(``RENTMATE_AUTH_TOKENS`` + ``AuthContext`` + ``manager_id_from_auth``)
ONLY. The request body carries no identity field: it supplies the
property, the period, a client-minted idempotency key (stored as the
trigger ``event_id``) and an optional reason. Retrying with the same
idempotency key reuses the existing report; a deliberate regeneration
mints a new key and produces the next generation.

Two read-only fetch endpoints are also served here: listing all stored
generations for a (property, period) and reading a single report by id.
Both are strictly scoped to the manager's verified property binding and
never collect data, call the LLM, or write anything. A manager without a
binding for the requested property is denied with the same response
whether or not the report exists, so the denial never leaks report
existence.
"""

from __future__ import annotations

import hmac
import json
import os
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict

from contracts.reporting import (
    CallerIdentity,
    GenerationRequest,
    ManagerReportTrigger,
    PropertyId,
    ReportGenerationError,
    ReportId,
    ReportPeriod,
    ReportRecord,
)
from lease_rent.reporting import SQLiteLeaseRentReportSource
from llm.reporting import LlmReportNarrator
from maintenance.reporting import SQLiteMaintenanceReportSource
from maintenance.store import Store
from permissions import (
    ManagerAccessDeniedError,
    MissingManagerIdentityError,
    SQLiteManagerRepository,
    manager_id_from_auth,
    require_manager_property_access,
)
from reporting.collector import ReportDataCollector
from reporting.repository import ReportRepository
from reporting.service import ReportService
from shared import AuthContext
from shared.sqlite import init_connection

router = APIRouter(prefix="/api/manager/monthly-reports", tags=["Manager Reporting"])
bearer = HTTPBearer()


def get_connection() -> Iterator[sqlite3.Connection]:
    path = os.getenv("DATABASE_PATH", "./rentmate.db")
    bootstrap = init_connection(path)
    bootstrap.close()
    # FastAPI executes synchronous dependencies/endpoints in a worker pool;
    # the request-scoped connection may therefore cross worker threads.
    connection = sqlite3.connect(path, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        yield connection
    finally:
        connection.close()


def get_auth(
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(bearer)],
    connection: Annotated[sqlite3.Connection, Depends(get_connection)],
) -> AuthContext:
    try:
        accounts = json.loads(os.getenv("RENTMATE_AUTH_TOKENS", "{}"))
        account = next(
            (
                value
                for token, value in accounts.items()
                if hmac.compare_digest(token, credentials.credentials)
            ),
            None,
        )
        if not account:
            raise ValueError()
        auth = AuthContext(
            tenant_id=account["tenant_id"],
            property_id=account["property_id"],
            manager_id=account.get("manager_id"),
        )
        manager_id = manager_id_from_auth(auth)
        require_manager_property_access(
            manager_id,
            auth.property_id,
            SQLiteManagerRepository(connection),
        )
        return auth
    except (MissingManagerIdentityError, ManagerAccessDeniedError):
        raise HTTPException(403, "Manager is not authorised for this property")
    except (ValueError, TypeError, KeyError, AttributeError):
        raise HTTPException(401, "Invalid or unconfigured prototype session")


def build_narrator():
    """Narrative generator seam. The default is the real LLM narrator
    (fail-closed when the LLM env is unconfigured); tests may override
    this with a marked fake so no model call is ever made."""
    return LlmReportNarrator()


def get_service(
    connection: Annotated[sqlite3.Connection, Depends(get_connection)],
) -> ReportService:
    database = Path(connection.execute("PRAGMA database_list").fetchone()[2])
    collector = ReportDataCollector(
        SQLiteMaintenanceReportSource(Store(database)),
        SQLiteLeaseRentReportSource(connection),
    )
    return ReportService(
        ReportRepository(connection), collector, build_narrator()
    )


class ManagerRunBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    property_id: str
    year: int
    month: int
    idempotency_key: str
    reason: str | None = None


@router.post("/generate")
def generate_monthly_report(
    body: ManagerRunBody,
    auth: Annotated[AuthContext, Depends(get_auth)],
    service: Annotated[ReportService, Depends(get_service)],
):
    manager_id = manager_id_from_auth(auth)
    try:
        period = ReportPeriod(year=body.year, month=body.month)
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    trigger = ManagerReportTrigger(
        manager_id=manager_id,
        event_id=body.idempotency_key,
        reason=body.reason,
    )
    result = service.generate(
        GenerationRequest(
            property_id=body.property_id, period=period, trigger=trigger
        ),
        caller=CallerIdentity(manager_id),
    )
    if isinstance(result, ReportGenerationError):
        status = {
            "authorization_denied": 403,
            "event_id_conflict": 409,
            "all_sources_unavailable": 503,
        }.get(result.code, 500)
        raise HTTPException(status, result.model_dump(mode="json"))
    return result


def _require_access(connection: sqlite3.Connection, manager_id: str, property_id: str) -> None:
    """Deny a manager that has no verified binding for the property.

    The denial deliberately does NOT distinguish "no binding" from "the
    report does not exist" — both surface as the same 404 below, so a
    manager can never probe whether a report exists for a property they
    are not bound to. The service layer re-checks access independently
    (defense in depth) using the same trusted identity source.
    """
    try:
        require_manager_property_access(manager_id, property_id, SQLiteManagerRepository(connection))
    except ManagerAccessDeniedError:
        raise HTTPException(404, "Report not found")


@router.get("", response_model=list[ReportRecord])
def list_reports(
    property_id: str,
    year: int,
    month: int,
    auth: Annotated[AuthContext, Depends(get_auth)],
    service: Annotated[ReportService, Depends(get_service)],
    connection: Annotated[sqlite3.Connection, Depends(get_connection)],
):
    """All stored generations for one (property, period), ascending.

    Read-only: no collection, no LLM call, no writes. Denial and absence
    are indistinguishable (both 404) so report existence never leaks.
    """
    manager_id = manager_id_from_auth(auth)
    _require_access(connection, manager_id, property_id)
    try:
        period = ReportPeriod(year=year, month=month)
    except ValueError:
        raise HTTPException(422, "Invalid period")
    caller = CallerIdentity(manager_id)
    return service.list_for_period(PropertyId(property_id), period, caller)


@router.get("/{report_id}", response_model=ReportRecord)
def get_report(
    report_id: str,
    auth: Annotated[AuthContext, Depends(get_auth)],
    service: Annotated[ReportService, Depends(get_service)],
    connection: Annotated[sqlite3.Connection, Depends(get_connection)],
):
    """A single report by id, scoped to the manager's property.

    The property is taken from the auth context (the manager's verified
    binding), so a report cannot be fetched on another property's
    behalf. Denial and absence are indistinguishable (both 404).
    """
    manager_id = manager_id_from_auth(auth)
    property_id = auth.property_id
    _require_access(connection, manager_id, property_id)
    caller = CallerIdentity(manager_id)
    record = service.get(ReportId(report_id), PropertyId(property_id), caller)
    if record is None:
        raise HTTPException(404, "Report not found")
    return record
