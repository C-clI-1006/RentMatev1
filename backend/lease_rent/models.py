"""Internal domain models for UC4 and UC5.

These types belong to the lease/rent module.  They intentionally do not change
the shared contracts while the team is still reviewing cross-module APIs.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import Enum


class RentStatus(str, Enum):
    """The three rent states agreed in the Stage 1 model."""

    PAID = "paid"
    UNPAID_NOT_DUE = "unpaid-but-not-due"
    OVERDUE = "overdue"


@dataclass(frozen=True, slots=True)
class RentAssessment:
    """Result of evaluating one rent record as at a given date."""

    status: RentStatus
    outstanding_amount: Decimal
    overdue_days: int

    @property
    def reminder_eligible(self) -> bool:
        return self.status is RentStatus.OVERDUE


class LeaseDeadlineStatus(str, Enum):
    """Reason why a lease is or is not eligible for a deadline alert."""

    ELIGIBLE = "eligible"
    INACTIVE = "inactive"
    OUTSIDE_REMINDER_WINDOW = "outside-reminder-window"
    ALERT_ALREADY_EXISTS = "alert-already-exists"


@dataclass(frozen=True, slots=True)
class LeaseDeadlineAssessment:
    """Result of evaluating one lease for UC5."""

    status: LeaseDeadlineStatus
    remaining_days: int

    @property
    def alert_eligible(self) -> bool:
        return self.status is LeaseDeadlineStatus.ELIGIBLE


@dataclass(frozen=True, slots=True)
class RentRecordInput:
    """Minimum structured facts required by the UC4 rule."""

    rent_record_id: str
    due_date: date
    amount_due: Decimal
    amount_paid: Decimal


@dataclass(frozen=True, slots=True)
class LeaseDeadlineInput:
    """Minimum structured facts required by the UC5 rule."""

    lease_id: str
    start_date: date
    end_date: date


@dataclass(frozen=True, slots=True)
class RentReminderCandidate:
    """Traceable UC4 data ready to be adapted into a reviewable draft."""

    candidate_key: str
    rent_record_id: str
    due_date: date
    outstanding_amount: Decimal
    overdue_days: int
    prepared_on: date


@dataclass(frozen=True, slots=True)
class LeaseDeadlineAlertCandidate:
    """Traceable UC5 data ready to be adapted into a reviewable alert."""

    candidate_key: str
    lease_id: str
    end_date: date
    remaining_days: int
    prepared_on: date


@dataclass(frozen=True, slots=True)
class VerifiedFactRecord:
    """One verified repository fact with enough metadata for provenance."""

    name: str
    value: str
    observed_at: datetime
    source_type: str
    source_id: str
    lease_id: str | None = None
    version: str | None = None
    snippet: str | None = None
    stale_after: timedelta | None = None

