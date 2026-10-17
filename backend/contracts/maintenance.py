"""Communication -> Maintenance contract (guide 18.1).

Caller: communication module (Chaney).
Provider: maintenance module (Kerwin).

Rules:
- `tenant_id` / `property_id` come from the auth context; the
  tenant<->property binding must already be verified by the caller.
- The three outcomes are MUTUALLY EXCLUSIVE. "Needs clarification" is
  not a failure: no request may have been created, but the tenant may
  still be asked for the missing fields (guide 11.3).
- Deduplication: a repeated intake with the same `event_id` must return
  the original outcome, not create a second request.
- Failure ownership: the maintenance module records its own failure;
  communication only reports what is returned here.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from enum import Enum
from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field

from .common import EventId, MessageId, PropertyId, RequestId, TenantId


class Priority(str, Enum):
    """Request priority. Emergency handling may notify the manager first;
    sentiment signals must never lower an emergency to a lower priority."""

    EMERGENCY = "emergency"
    HIGH = "high"
    NORMAL = "normal"
    LOW = "low"


class MaintenanceStatus(str, Enum):
    """Persisted status of a maintenance request.

    NOTE: request status and delivery/approval status are separate state
    machines and must not be merged (guide 12.2).
    """

    SUBMITTED = "submitted"
    TRIAGE = "triage"
    ASSIGNED = "assigned"
    IN_PROGRESS = "in_progress"
    RESOLVED = "resolved"
    CLOSED = "closed"
    REJECTED = "rejected"


class MaintenanceIntake(BaseModel):
    """A bounded, validated maintenance request produced by communication."""

    message_id: MessageId
    tenant_id: TenantId  # from auth context
    property_id: PropertyId  # from auth context; binding verified
    title: str
    description: str
    location: str | None = None  # e.g. "2F kitchen"
    event_id: EventId  # stable dedup key
    submitted_at: datetime  # UTC


class RegisteredIntake(BaseModel):
    status: Literal["registered"] = "registered"
    request_id: RequestId
    persisted_status: MaintenanceStatus


class NeedsClarificationIntake(BaseModel):
    """Missing required detail. NOT a failure: no request was created,
    but the tenant may be asked for the missing fields."""

    status: Literal["needs_clarification"] = "needs_clarification"
    missing_fields: list[str]


class FailedIntake(BaseModel):
    status: Literal["failed"] = "failed"
    code: str  # stable machine-readable failure code
    retryable: bool
    detail: str | None = None


IntakeResult = Annotated[
    Union[RegisteredIntake, NeedsClarificationIntake, FailedIntake],
    Field(discriminator="status"),
]


class IMaintenanceIntake(ABC):
    """Public interface for the maintenance module."""

    @abstractmethod
    def submit_issue(self, intake: MaintenanceIntake) -> IntakeResult:
        """Register (or clarify / fail) a maintenance request.

        Must be idempotent on `event_id`: a duplicate returns the
        original outcome (e.g. the same `request_id` if already
        registered).
        """
