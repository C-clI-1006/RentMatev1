"""Maintenance domain contract (guide 18.1, 12.2).

Aligned to Stage 1 report:
- §5.3.3 UC3 specification (states, guards, alternative flows A1-A8)
- §8.3.2 UC3 state machine diagram
- §3.2 Maintenance Coordinator Agent (Core Feature 1 & 2)

Intake (UC1 hand-off):
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

Tracking (UC3):
  Caller: manager UI / reporting module.
  Provider: maintenance module (Kerwin).

Rules:
- Status transitions are guarded by the UC3 state machine (§8.3.2).
- `expected_version` provides optimistic concurrency control (A5: a
  stale command is rejected and the latest state is returned; nothing
  is silently overwritten).
- Moving to AwaitingConfirmation and to Closed requires at least one
  evidence reference (completion evidence guard, A6).
- Every accepted change atomically stores the new state, one append-only
  `MaintenanceEvent` and the outbox dispatch entries.
- The LLM priority assessment is a *recommendation*; the deterministic
  state machine controls what is persisted (A2: uncertainty routes to
  UnderReview, never inferred approval).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from enum import Enum
from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field

from .common import EventId, MessageId, PropertyId, RequestId, TenantId


class Priority(str, Enum):
    """Assessed priority (§5.3.3, FR-MC-02).

    Emergency handling alerts the manager immediately; sentiment or
    other signals must never lower an Emergency to a lower priority.
    """

    EMERGENCY = "emergency"
    URGENT = "urgent"
    ROUTINE = "routine"


class MaintenanceStatus(str, Enum):
    """UC3 request lifecycle (§5.3.3, §8.3.2).

    NOTE: request status and delivery/approval status are separate state
    machines and must not be merged (guide 12.2).
    """

    SUBMITTED = "submitted"
    ASSESSED = "assessed"
    UNDER_REVIEW = "under_review"
    APPROVED = "approved"
    IN_PROGRESS = "in_progress"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    CLOSED = "closed"
    CANCELLED = "cancelled"


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
    priority: Priority  # assessed at registration (FR-MC-02)


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
    """Public interface for the maintenance module (UC1 intake side)."""

    @abstractmethod
    def submit_issue(self, intake: MaintenanceIntake) -> IntakeResult:
        """Register (or clarify / fail) a maintenance request.

        Must be idempotent on `event_id`: a duplicate returns the
        original outcome (e.g. the same `request_id` if already
        registered).
        """


# ---------------------------------------------------------------------------
# UC3 tracking contract (manager-driven status transitions)
# ---------------------------------------------------------------------------


class TransitionFailureCode(str, Enum):
    """Stable machine-readable failure codes for status commands."""

    NOT_FOUND = "not_found"
    VERSION_MISMATCH = "version_mismatch"  # A5: stale expectedVersion
    ILLEGAL_TRANSITION = "illegal_transition"
    EVIDENCE_REQUIRED = "evidence_required"  # A6: completion/closure guard
    FORBIDDEN = "forbidden"  # A1: unauthorised / unlinked


class MaintenanceStatusCommand(BaseModel):
    """Manager → Maintenance (§5.3.3 Table 5.6).

    Membership and transition guards are validated on the server.
    `expected_version` is the version the manager last saw; a stale
    command fails with VERSION_MISMATCH and the current state is
    returned for review (A5).
    `evidence_refs` is mandatory when moving to AwaitingConfirmation
    or Closed (completion evidence guard, A6).
    """

    request_id: RequestId
    target_status: MaintenanceStatus
    expected_version: int = Field(ge=1)
    manager_id: str
    reason: str | None = None
    evidence_refs: list[str] = Field(default_factory=list)

    def requires_evidence(self) -> bool:
        return self.target_status in (
            MaintenanceStatus.AWAITING_CONFIRMATION,
            MaintenanceStatus.CLOSED,
        )


class StatusResultOk(BaseModel):
    status: Literal["applied"] = "applied"
    request_id: RequestId
    persisted_status: MaintenanceStatus
    version: int  # new version after the transition
    event: "MaintenanceEvent"


class StatusResultError(BaseModel):
    status: Literal["failed"] = "failed"
    code: TransitionFailureCode
    detail: str | None = None
    current_status: MaintenanceStatus | None = None  # present on A5 / A1
    current_version: int | None = None


StatusResult = Annotated[
    Union[StatusResultOk, StatusResultError],
    Field(discriminator="status"),
]


class MaintenanceEvent(BaseModel):
    """Maintenance → evidence / communication (§5.3.3 Table 5.6).

    Append-only status-change fact. Consumers deduplicate by
    `event_id` and link the existing evidence record.
    """

    event_id: EventId
    request_id: RequestId
    property_id: PropertyId
    occurred_at: datetime  # UTC
    actor: str  # "tenant:<id>" | "manager:<id>" | "system:<component>"
    from_status: MaintenanceStatus | None  # None on initial registration
    to_status: MaintenanceStatus
    priority: Priority
    reason: str | None = None
    evidence_refs: list[str] = Field(default_factory=list)


class MaintenanceView(BaseModel):
    """Read model for one request: current state + full transition history."""

    request_id: RequestId
    tenant_id: TenantId
    property_id: PropertyId
    message_id: MessageId
    title: str
    description: str
    location: str | None = None
    priority: Priority
    status: MaintenanceStatus
    version: int
    emergency_flag: bool = False
    provider_suggestion: str | None = None  # optional FR-MC-08
    created_at: datetime  # UTC
    updated_at: datetime  # UTC
    events: list[MaintenanceEvent] = Field(default_factory=list)


class IMaintenanceTracking(ABC):
    """Public interface for the maintenance module (UC3 tracking side)."""

    @abstractmethod
    def change_status(self, command: MaintenanceStatusCommand) -> StatusResult:
        """Apply a guarded status transition (optimistic locking, A5)."""

    @abstractmethod
    def get(self, request_id: RequestId, property_id: PropertyId) -> MaintenanceView | None:
        """Property-scoped read. Returns None if absent or out of scope."""
