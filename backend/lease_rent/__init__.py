"""Lease & Rent module (owner: Ziheng).

The deterministic UC4/UC5 domain rules are implemented here. Persistence and
cross-module adapters are added separately after the shared contracts have been
reviewed by the affected owners.
"""

from .fact_query import LeaseRentFactRepository, LeaseRentQueryService
from .models import (
    LeaseDeadlineAlertCandidate,
    LeaseDeadlineAssessment,
    LeaseDeadlineInput,
    LeaseDeadlineStatus,
    RentAssessment,
    RentRecordInput,
    RentReminderCandidate,
    RentStatus,
    VerifiedFactRecord,
)
from .rules import (
    assess_lease_deadline,
    assess_rent,
    lease_deadline_alert_key,
    rent_reminder_key,
)
from .service import (
    prepare_deadline_alert,
    prepare_deadline_alert_batch,
    prepare_overdue_rent_batch,
    prepare_rent_reminder,
)

__all__ = [
    "LeaseRentFactRepository",
    "LeaseRentQueryService",
    "LeaseDeadlineAlertCandidate",
    "LeaseDeadlineAssessment",
    "LeaseDeadlineInput",
    "LeaseDeadlineStatus",
    "RentAssessment",
    "RentRecordInput",
    "RentReminderCandidate",
    "RentStatus",
    "VerifiedFactRecord",
    "assess_lease_deadline",
    "assess_rent",
    "lease_deadline_alert_key",
    "prepare_deadline_alert",
    "prepare_deadline_alert_batch",
    "prepare_overdue_rent_batch",
    "prepare_rent_reminder",
    "rent_reminder_key",
]
