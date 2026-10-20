"""RentMate shared contracts.

Single source of truth for all cross-module data types and service
interfaces. Every module imports from here; modules must not redefine
these shapes locally or touch another module's private data.

Governance: see README.md in this package.
"""

from .common import (
    AuditId,
    DraftId,
    EventId,
    LeaseId,
    MessageId,
    PropertyId,
    RecipientId,
    RentRecordId,
    RequestId,
    ReviewId,
    TenantId,
)
from .maintenance import (
    FailedIntake,
    IMaintenanceIntake,
    IntakeResult,
    MaintenanceIntake,
    MaintenanceStatus,
    NeedsClarificationIntake,
    Priority,
    RegisteredIntake,
)
from .lease_rent import (
    Fact,
    FactAvailability,
    FactReference,
    ILeaseRentQuery,
    LeaseRentQuery,
    QuestionType,
)
from .drafting import (
    Draft,
    DraftSourceKind,
    DraftStatus,
    ReviewDecisionRecord,
    ReviewRequest,
)
from .notification import (
    DeliveryResult,
    INotificationService,
    NotificationEvent,
    NotificationKind,
    NotificationStatus,
)
from .audit import AuditEvent

__all__ = [
    # common
    "AuditId",
    "DraftId",
    "EventId",
    "LeaseId",
    "MessageId",
    "PropertyId",
    "RecipientId",
    "RentRecordId",
    "RequestId",
    "ReviewId",
    "TenantId",
    # maintenance
    "FailedIntake",
    "IMaintenanceIntake",
    "IntakeResult",
    "MaintenanceIntake",
    "MaintenanceStatus",
    "NeedsClarificationIntake",
    "Priority",
    "RegisteredIntake",
    # lease & rent
    "Fact",
    "FactAvailability",
    "FactReference",
    "ILeaseRentQuery",
    "LeaseRentQuery",
    "QuestionType",
    # drafting / review
    "Draft",
    "DraftSourceKind",
    "DraftStatus",
    "ReviewDecisionRecord",
    "ReviewRequest",
    # notification
    "INotificationService",
    "NotificationEvent",
    "NotificationKind",
    "NotificationStatus",
    # audit
    "AuditEvent",
]
