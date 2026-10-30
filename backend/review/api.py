"""Authenticated HTTP façade for manager draft review."""

from __future__ import annotations

import hmac
import json
import os
import sqlite3
from collections.abc import Iterator
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field

from contracts import ReviewDecisionRecord
from contracts.drafting import ReviewDecision
from permissions import (
    ManagerAccessDeniedError,
    MissingManagerIdentityError,
    SQLiteManagerRepository,
    manager_id_from_auth,
    require_manager_property_access,
)
from review.service import (
    InvalidReviewDecisionError,
    ReviewConflictError,
    ReviewNotFoundError,
    ReviewQueueItem,
    ReviewService,
)
from shared import AuthContext
from shared.sqlite import init_connection
from .handoffs import HandoffCase, HandoffService

router = APIRouter(prefix="/api/reviews", tags=["Manager Review"])
bearer = HTTPBearer()


def get_connection(request: Request) -> Iterator[sqlite3.Connection]:
    path = request.app.state.db_path
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


def get_service(
    connection: Annotated[sqlite3.Connection, Depends(get_connection)],
    auth: Annotated[AuthContext, Depends(get_auth)],
) -> ReviewService:
    return ReviewService(connection, auth)


class DecisionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1)
    decision: ReviewDecision
    final_content: str | None = None
    reason: str | None = None


class HandoffResolutionBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    outcome: Literal['resolved', 'dismissed']
    resolution: str = Field(min_length=1, max_length=2000)


def get_handoffs(
    connection: Annotated[sqlite3.Connection, Depends(get_connection)],
    auth: Annotated[AuthContext, Depends(get_auth)],
) -> HandoffService:
    return HandoffService(connection, auth)


@router.get('/handoffs', response_model=list[HandoffCase])
def list_handoffs(service: Annotated[HandoffService, Depends(get_handoffs)]):
    return service.list_cases()


@router.get('/handoffs/{case_id}', response_model=HandoffCase)
def get_handoff(case_id: str, service: Annotated[HandoffService, Depends(get_handoffs)]):
    try:
        return service.get_case(case_id)
    except ReviewNotFoundError:
        raise HTTPException(404, 'Handoff case not found')


@router.post('/handoffs/{case_id}/resolution', response_model=HandoffCase)
def resolve_handoff(case_id: str, body: HandoffResolutionBody,
                    service: Annotated[HandoffService, Depends(get_handoffs)]):
    try:
        return service.settle(case_id, **body.model_dump())
    except ReviewNotFoundError:
        raise HTTPException(404, 'Handoff case not found')
    except ReviewConflictError as error:
        raise HTTPException(409, str(error))
    except InvalidReviewDecisionError as error:
        raise HTTPException(422, str(error))


@router.get("", response_model=list[ReviewQueueItem])
def list_pending(service: Annotated[ReviewService, Depends(get_service)]):
    return service.list_pending()


@router.get("/{draft_id}", response_model=ReviewQueueItem)
def get_pending(draft_id: str, service: Annotated[ReviewService, Depends(get_service)]):
    try:
        return service.get_pending(draft_id)
    except ReviewNotFoundError:
        raise HTTPException(404, "Pending review draft not found")


@router.post("/{draft_id}/decision", response_model=ReviewDecisionRecord)
def decide(
    draft_id: str,
    body: DecisionBody,
    service: Annotated[ReviewService, Depends(get_service)],
):
    try:
        return service.decide(draft_id, **body.model_dump())
    except ReviewNotFoundError:
        raise HTTPException(404, "Draft not found")
    except ReviewConflictError as error:
        raise HTTPException(409, str(error))
    except InvalidReviewDecisionError as error:
        raise HTTPException(422, str(error))
