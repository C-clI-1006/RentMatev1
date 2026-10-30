"""Deterministic eligibility and date rules for UC4 and UC5."""

from datetime import date
from decimal import Decimal

from .models import (
    LeaseDeadlineAssessment,
    LeaseDeadlineInput,
    LeaseDeadlineStatus,
    RentAssessment,
    RentRecordInput,
    LeaseRentRisk,
    RiskSeverity,
    RentStatus,
)


def assess_rent_risk(record: RentRecordInput, *, as_of: date) -> LeaseRentRisk | None:
    """Return the deterministic UC4 risk, if this rent record is overdue."""

    assessment = assess_rent(record, as_of=as_of)
    if not assessment.reminder_eligible:
        return None
    return LeaseRentRisk(
        rule_id="risk.overdue_rent",
        severity=RiskSeverity.HIGH,
        source_type="rent_record",
        source_id=record.rent_record_id,
        summary="Rent record is overdue",
    )


def assess_lease_risk(
    lease: LeaseDeadlineInput,
    *,
    as_of: date,
    reminder_window_days: int,
) -> LeaseRentRisk | None:
    """Return the deterministic UC5 risk, if a lease is nearing expiry."""

    assessment = assess_lease_deadline(
        lease,
        as_of=as_of,
        reminder_window_days=reminder_window_days,
        alert_already_exists=False,
    )
    if not assessment.alert_eligible:
        return None
    return LeaseRentRisk(
        rule_id="risk.lease_expiring",
        severity=RiskSeverity.MEDIUM,
        source_type="lease",
        source_id=lease.lease_id,
        summary="Lease expires within the reminder threshold",
    )


def assess_rent(record: RentRecordInput, *, as_of: date) -> RentAssessment:
    """Classify a rent record and calculate its outstanding balance.

    A partially paid amount is supported as data, but it is not a separate
    lifecycle state: an outstanding balance is either not yet due or overdue.
    The due date itself is not overdue; overdue days start on the next day.
    """

    _validate_amounts(record.amount_due, record.amount_paid)

    outstanding = max(record.amount_due - record.amount_paid, Decimal("0"))
    if outstanding == 0:
        return RentAssessment(
            status=RentStatus.PAID,
            outstanding_amount=outstanding,
            overdue_days=0,
        )

    overdue_days = max((as_of - record.due_date).days, 0)
    if overdue_days > 0:
        return RentAssessment(
            status=RentStatus.OVERDUE,
            outstanding_amount=outstanding,
            overdue_days=overdue_days,
        )

    return RentAssessment(
        status=RentStatus.UNPAID_NOT_DUE,
        outstanding_amount=outstanding,
        overdue_days=0,
    )


def assess_lease_deadline(
    lease: LeaseDeadlineInput,
    *,
    as_of: date,
    reminder_window_days: int,
    alert_already_exists: bool,
) -> LeaseDeadlineAssessment:
    """Evaluate UC5 eligibility for an upcoming lease deadline alert.

    Eligibility requires an active lease, an end date inside the inclusive
    reminder window, and no existing alert for the same alert key.
    """

    if lease.end_date < lease.start_date:
        raise ValueError("lease end_date cannot be before start_date")
    if reminder_window_days < 0:
        raise ValueError("reminder_window_days cannot be negative")

    remaining_days = (lease.end_date - as_of).days
    if as_of < lease.start_date or as_of > lease.end_date:
        return LeaseDeadlineAssessment(
            status=LeaseDeadlineStatus.INACTIVE,
            remaining_days=remaining_days,
        )

    if remaining_days > reminder_window_days:
        return LeaseDeadlineAssessment(
            status=LeaseDeadlineStatus.OUTSIDE_REMINDER_WINDOW,
            remaining_days=remaining_days,
        )

    if alert_already_exists:
        return LeaseDeadlineAssessment(
            status=LeaseDeadlineStatus.ALERT_ALREADY_EXISTS,
            remaining_days=remaining_days,
        )

    return LeaseDeadlineAssessment(
        status=LeaseDeadlineStatus.ELIGIBLE,
        remaining_days=remaining_days,
    )


def rent_reminder_key(record: RentRecordInput) -> str:
    """Return a stable key used to prevent duplicate reminders for a record."""

    return f"rent-overdue:{record.rent_record_id}:{record.due_date.isoformat()}"


def lease_deadline_alert_key(lease: LeaseDeadlineInput) -> str:
    """Return a stable key used to prevent duplicate deadline alerts."""

    return f"lease-deadline:{lease.lease_id}:{lease.end_date.isoformat()}"


def _validate_amounts(amount_due: Decimal, amount_paid: Decimal) -> None:
    if amount_due < 0:
        raise ValueError("amount_due cannot be negative")
    if amount_paid < 0:
        raise ValueError("amount_paid cannot be negative")
