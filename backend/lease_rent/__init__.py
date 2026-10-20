"""Lease & Rent module (owner: Ziheng).

The deterministic UC4/UC5 domain rules are implemented here. Persistence and
cross-module adapters are added separately after the shared contracts have been
reviewed by the affected owners.
"""

from .models import (
    LeaseDeadlineAssessment,
    LeaseDeadlineInput,
    LeaseDeadlineStatus,
    RentAssessment,
    RentRecordInput,
    RentStatus,
)
from .rules import (
    assess_lease_deadline,
    assess_rent,
    lease_deadline_alert_key,
    rent_reminder_key,
)

__all__ = [
    "LeaseDeadlineAssessment",
    "LeaseDeadlineInput",
    "LeaseDeadlineStatus",
    "RentAssessment",
    "RentRecordInput",
    "RentStatus",
    "assess_lease_deadline",
    "assess_rent",
    "lease_deadline_alert_key",
    "rent_reminder_key",
]
