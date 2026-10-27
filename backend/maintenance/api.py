import hashlib
import hmac
import json
import os
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field

from contracts.maintenance import (
    MaintenanceIntake,
    MaintenanceStatus,
    MaintenanceStatusCommand,
    Priority,
)
from llm.live import LlmPriorityAssessor
from maintenance.service import MaintenanceService
from maintenance.store import Store
from permissions import (
    ManagerAccessDeniedError,
    MissingManagerIdentityError,
    SQLiteManagerRepository,
    manager_id_from_auth,
    require_manager_property_access,
)
from shared import AuthContext, new_id, utcnow
from shared.sqlite import init_connection

router = APIRouter(prefix="/api/maintenance", tags=["Maintenance (Kerwin)"])
bearer = HTTPBearer()


def get_store(request: Request) -> Store:
    path = request.app.state.db_path
    # The API uses the shared identity/binding tables as its trust boundary.
    # Maintenance still writes only its own mc_* tables.
    bootstrap = init_connection(path)
    bootstrap.close()
    return Store(path)


def get_auth(
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(bearer)],
    store: Annotated[Store, Depends(get_store)],
):
    # Prototype bridge; replace with the group's verified session resolver.
    try:
        accounts = json.loads(os.getenv("RENTMATE_AUTH_TOKENS", "{}"))
        account = next(
            (v for k, v in accounts.items() if hmac.compare_digest(k, credentials.credentials)),
            None,
        )
        if not account:
            raise ValueError()
        auth = AuthContext(
            tenant_id=account["tenant_id"],
            property_id=account["property_id"],
            manager_id=account.get("manager_id"),
        )
        with store.transaction() as db:
            if auth.manager_id:
                require_manager_property_access(
                    manager_id_from_auth(auth),
                    auth.property_id,
                    SQLiteManagerRepository(db),
                )
            elif db.execute(
                "SELECT 1 FROM tenant_property WHERE tenant_id=? AND property_id=?",
                (auth.tenant_id, auth.property_id),
            ).fetchone() is None:
                raise PermissionError("tenant/property binding not verified")
        return auth
    except (ManagerAccessDeniedError, MissingManagerIdentityError, PermissionError):
        raise HTTPException(403, "Session is not authorised for this property")
    except (ValueError, TypeError, KeyError, AttributeError):
        raise HTTPException(401, "Invalid or unconfigured prototype session")


def get_service(
    auth: Annotated[AuthContext, Depends(get_auth)], store: Annotated[Store, Depends(get_store)]
):
    return MaintenanceService(store, LlmPriorityAssessor(), auth)


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class IntakeBody(Input):
    title: str
    description: str
    location: str | None = None
    evidence_refs: list[str] = Field(default_factory=list)


class CommandBody(Input):
    target_status: MaintenanceStatus
    expected_version: int = Field(ge=1)
    reason: str = Field(min_length=1)
    evidence_refs: list[str] = Field(default_factory=list)


class ManualBody(Input):
    expected_version: int = Field(ge=1)
    priority: Priority
    rationale: str = Field(min_length=1)


def manager(service):
    try:
        service.require_manager_access()
    except (ManagerAccessDeniedError, MissingManagerIdentityError, PermissionError):
        raise HTTPException(403, "Manager session required")


def command_result(result):
    if result.status == "failed":
        code = {"not_found": 404, "forbidden": 403, "version_mismatch": 409}.get(
            result.code.value, 422
        )
        raise HTTPException(code, detail=result.model_dump(mode="json"))
    return result


@router.post("/requests")
def intake(
    body: IntakeBody, service: Annotated[MaintenanceService, Depends(get_service)],
    idempotency_key: Annotated[str | None, Header(min_length=1, max_length=128)] = None,
):
    message_id = new_id()
    if idempotency_key is not None:
        scope = json.dumps([service.auth.property_id, service.auth.tenant_id, idempotency_key])
        message_id = "http:" + hashlib.sha256(scope.encode()).hexdigest()
    result = service.submit_issue(
        MaintenanceIntake(
            **body.model_dump(),
            message_id=message_id,
            event_id="intake:" + message_id,
            tenant_id=service.auth.tenant_id,
            property_id=service.auth.property_id,
            submitted_at=utcnow(),
        ),
        strict_replay=idempotency_key is not None,
    )
    if result.status == "failed":
        code = {"forbidden": 403, "idempotency_conflict": 409}.get(result.code, 503)
        raise HTTPException(code, result.model_dump())
    return result


@router.get("/requests")
def requests(service: Annotated[MaintenanceService, Depends(get_service)]):
    return service.list_requests()


@router.get("/summaries")
def summaries(service: Annotated[MaintenanceService, Depends(get_service)]):
    manager(service)
    return service.summaries()


@router.get("/outbox")
def outbox(service: Annotated[MaintenanceService, Depends(get_service)]):
    manager(service)
    with service.store.transaction() as db:
        return [
            dict(r)
            for r in db.execute(
                "SELECT event_id,target,request_id,status,attempts,last_error FROM mc_outbox WHERE property_id=?",
                (service.auth.property_id,),
            )
        ]


@router.get("/requests/{request_id}")
def request(request_id: str, service: Annotated[MaintenanceService, Depends(get_service)]):
    value = service.get(request_id, service.auth.property_id)
    if value is None:
        raise HTTPException(404, "Request not found")
    return value


@router.post("/requests/{request_id}/status")
def change(
    request_id: str, body: CommandBody, service: Annotated[MaintenanceService, Depends(get_service)]
):
    manager(service)
    return command_result(
        service.change_status(
            MaintenanceStatusCommand(
                request_id=request_id, manager_id=service.auth.manager_id, **body.model_dump()
            )
        )
    )


@router.post("/requests/{request_id}/assessment")
def manual(
    request_id: str, body: ManualBody, service: Annotated[MaintenanceService, Depends(get_service)]
):
    manager(service)
    return command_result(service.manual_assessment(request_id, **body.model_dump()))


@router.get("/demo", include_in_schema=False)
def demo():
    return FileResponse(Path(__file__).with_name("demo.html"))
