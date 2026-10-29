"""Authenticated HTTP entry point for tenant communication.

The request body contains message content only. Tenant and property scope are
resolved from the server-side bearer-token mapping and verified against the
authoritative ``tenant_property`` binding before any business service runs.
"""

from __future__ import annotations

import hmac
import json
import os
import sqlite3
from collections.abc import Iterator
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field

from lease_rent import LeaseRentQueryService, SQLiteLeaseRentRepository
from llm.live import LlmPriorityAssessor
from llm.classifier import build_communication_classifier
from maintenance.service import MaintenanceService
from maintenance.store import Store
from notification.service import SQLiteNotificationService
from reporting.api import require_scheduler
from shared import AuthContext
from shared.sqlite import init_connection

from .maintenance import MaintenanceCommunicationReceiver
from .service import CommunicationService


router = APIRouter(prefix="/api/communication", tags=["Tenant Communication"])
ops_router = APIRouter(
    prefix="/api/ops/communication", tags=["Communication Operations"]
)
bearer = HTTPBearer()


class MessageBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str = Field(min_length=1, max_length=10_000)


class MessageResponse(BaseModel):
    message_id: str
    reply_status: str
    delivery_status: Literal["not_dispatched"]


def get_connection(request: Request) -> Iterator[sqlite3.Connection]:
    connection = init_connection(request.app.state.db_path)
    try:
        yield connection
    finally:
        connection.close()


def get_auth(
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(bearer)],
    connection: Annotated[sqlite3.Connection, Depends(get_connection)],
) -> AuthContext:
    """Resolve and verify identity without accepting caller-supplied scope."""

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
        bound = connection.execute(
            "SELECT 1 FROM tenant_property WHERE tenant_id=? AND property_id=?",
            (auth.tenant_id, auth.property_id),
        ).fetchone()
        if bound is None:
            raise PermissionError()
        return auth
    except PermissionError:
        raise HTTPException(403, "Tenant is not authorised for this property")
    except (ValueError, TypeError, KeyError, AttributeError, json.JSONDecodeError):
        raise HTTPException(401, "Invalid or unconfigured prototype session")


def get_service(
    request: Request,
    connection: Annotated[sqlite3.Connection, Depends(get_connection)],
    auth: Annotated[AuthContext, Depends(get_auth)],
) -> CommunicationService:
    maintenance = MaintenanceService(
        Store(request.app.state.db_path),
        LlmPriorityAssessor(),
        auth,
    )
    lease_rent = LeaseRentQueryService(SQLiteLeaseRentRepository(connection))
    notification = SQLiteNotificationService(connection)
    return CommunicationService(
        connection,
        maintenance,
        lease_rent,
        notification,
        classifier=build_communication_classifier(),
    )


@router.post("/messages", response_model=MessageResponse)
def submit_message(
    body: MessageBody,
    service: Annotated[CommunicationService, Depends(get_service)],
    auth: Annotated[AuthContext, Depends(get_auth)],
) -> dict:
    try:
        return service.handle_message(auth, body.content).tenant_response()
    except PermissionError:
        # The entry-point check is repeated inside the service so a binding
        # removed between dependency resolution and execution still fails shut.
        raise HTTPException(403, "Tenant is not authorised for this property")


@ops_router.post(
    "/maintenance-events/run",
    dependencies=[Depends(require_scheduler)],
)
def receive_maintenance_events(
    request: Request,
    connection: Annotated[sqlite3.Connection, Depends(get_connection)],
    limit: int = Query(default=100, ge=1, le=1000),
) -> dict:
    """Run the trusted Maintenance-to-Communication outbox handoff.

    A successful handoff only creates a durable Communication receipt and
    draft. Tenant delivery remains the Notification dispatcher's separate
    responsibility, so queue state stays truthful across either failure.
    """

    from maintenance.outbox import dispatch_pending

    results = dispatch_pending(
        Store(request.app.state.db_path),
        {"communication": MaintenanceCommunicationReceiver(connection).receive},
        limit=limit,
    )
    counts = {"handed_off": 0, "retry": 0, "rejected": 0, "uncertain": 0}
    for result in results:
        counts[result["status"]] += 1
    return {"processed": len(results), **counts}
