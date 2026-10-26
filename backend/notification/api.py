"""HTTP façade for the executable prototype notification inbox."""

from __future__ import annotations

import hmac
import json
import os
import sqlite3
from collections.abc import Iterator
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from reporting.api import require_scheduler
from shared import AuthContext
from shared.sqlite import init_connection

from .dispatcher import SQLiteNotificationInboxAdapter, dispatch_pending


router = APIRouter(prefix="/api/notifications", tags=["Tenant Notifications"])
ops_router = APIRouter(prefix="/api/ops/notifications", tags=["Notification Operations"])
bearer = HTTPBearer()


class InboxItem(BaseModel):
    event_id: str
    content: str
    delivered_at: str


def get_connection(request: Request) -> Iterator[sqlite3.Connection]:
    connection = init_connection(request.app.state.db_path)
    try:
        yield connection
    finally:
        connection.close()


def get_tenant_auth(
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
        bound = connection.execute(
            "SELECT 1 FROM tenant_property WHERE tenant_id=? AND property_id=?",
            (auth.tenant_id, auth.property_id),
        ).fetchone()
        if bound is None:
            raise PermissionError()
        return auth
    except PermissionError:
        raise HTTPException(403, "Tenant is not authorised for this property")
    except (ValueError, TypeError, KeyError, AttributeError):
        raise HTTPException(401, "Invalid or unconfigured prototype session")


@router.get("/inbox", response_model=list[InboxItem])
def list_inbox(
    auth: Annotated[AuthContext, Depends(get_tenant_auth)],
    connection: Annotated[sqlite3.Connection, Depends(get_connection)],
):
    rows = connection.execute(
        "SELECT event_id,content,delivered_at FROM tenant_inbox "
        "WHERE recipient_id=? AND property_id=? "
        "ORDER BY delivered_at,event_id",
        (auth.tenant_id, auth.property_id),
    ).fetchall()
    return [InboxItem(**dict(row)) for row in rows]


@ops_router.post("/dispatch", dependencies=[Depends(require_scheduler)])
def dispatch_notifications(
    connection: Annotated[sqlite3.Connection, Depends(get_connection)],
    limit: int = Query(default=100, ge=1, le=1000),
):
    results = dispatch_pending(
        connection,
        SQLiteNotificationInboxAdapter(connection),
        limit=limit,
    )
    counts = {"delivered": 0, "pending_retry": 0, "failed": 0}
    for result in results:
        if result.status.value in counts:
            counts[result.status.value] += 1
    return {"processed": len(results), **counts}
