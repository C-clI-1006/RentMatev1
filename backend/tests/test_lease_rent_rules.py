"""Unit tests for the UC4 and UC5 deterministic rules."""

from datetime import date
from decimal import Decimal

import pytest

from lease_rent import (
    LeaseDeadlineInput,
    LeaseDeadlineStatus,
    RentRecordInput,
    RentStatus,
    assess_lease_deadline,
    assess_rent,
    lease_deadline_alert_key,
    rent_reminder_key,
)


def _rent(*, due: str, amount_due: str = "1000", amount_paid: str = "0"):
    return RentRecordInput(
        rent_record_id="rent-1",
        due_date=date.fromisoformat(due),
        amount_due=Decimal(amount_due),
        amount_paid=Decimal(amount_paid),
    )


def _lease(*, start: str = "2026-01-01", end: str = "2026-12-31"):
    return LeaseDeadlineInput(
        lease_id="lease-1",
        start_date=date.fromisoformat(start),
        end_date=date.fromisoformat(end),
    )


def test_uc4_fully_paid_record_is_paid_even_after_due_date():
    result = assess_rent(
        _rent(due="2026-10-01", amount_paid="1000"),
        as_of=date(2026, 10, 20),
    )

    assert result.status is RentStatus.PAID
    assert result.outstanding_amount == Decimal("0")
    assert result.overdue_days == 0
    assert not result.reminder_eligible


@pytest.mark.parametrize("as_of", [date(2026, 9, 30), date(2026, 10, 1)])
def test_uc4_outstanding_balance_before_or_on_due_date_is_not_due(as_of):
    result = assess_rent(_rent(due="2026-10-01"), as_of=as_of)

    assert result.status is RentStatus.UNPAID_NOT_DUE
    assert result.overdue_days == 0
    assert not result.reminder_eligible


def test_uc4_outstanding_balance_after_due_date_is_overdue():
    result = assess_rent(
        _rent(due="2026-10-01", amount_paid="250"),
        as_of=date(2026, 10, 20),
    )

    assert result.status is RentStatus.OVERDUE
    assert result.outstanding_amount == Decimal("750")
    assert result.overdue_days == 19
    assert result.reminder_eligible


def test_uc4_partial_amount_is_not_a_separate_status():
    statuses = {
        assess_rent(
            _rent(due="2026-10-21", amount_paid="250"),
            as_of=date(2026, 10, 20),
        ).status,
        assess_rent(
            _rent(due="2026-10-01", amount_paid="250"),
            as_of=date(2026, 10, 20),
        ).status,
    }

    assert statuses == {RentStatus.UNPAID_NOT_DUE, RentStatus.OVERDUE}


def test_uc4_rejects_negative_amounts():
    with pytest.raises(ValueError, match="amount_paid"):
        assess_rent(
            _rent(due="2026-10-01", amount_paid="-1"),
            as_of=date(2026, 10, 20),
        )


def test_uc5_active_lease_inside_window_without_alert_is_eligible():
    result = assess_lease_deadline(
        _lease(end="2026-11-19"),
        as_of=date(2026, 10, 20),
        reminder_window_days=30,
        alert_already_exists=False,
    )

    assert result.status is LeaseDeadlineStatus.ELIGIBLE
    assert result.remaining_days == 30
    assert result.alert_eligible


def test_uc5_active_lease_outside_window_is_not_eligible():
    result = assess_lease_deadline(
        _lease(end="2026-12-31"),
        as_of=date(2026, 10, 20),
        reminder_window_days=30,
        alert_already_exists=False,
    )

    assert result.status is LeaseDeadlineStatus.OUTSIDE_REMINDER_WINDOW
    assert not result.alert_eligible


@pytest.mark.parametrize(
    ("lease", "as_of"),
    [
        (_lease(start="2026-11-01"), date(2026, 10, 20)),
        (_lease(end="2026-10-19"), date(2026, 10, 20)),
    ],
)
def test_uc5_inactive_lease_is_not_eligible(lease, as_of):
    result = assess_lease_deadline(
        lease,
        as_of=as_of,
        reminder_window_days=30,
        alert_already_exists=False,
    )

    assert result.status is LeaseDeadlineStatus.INACTIVE
    assert not result.alert_eligible


def test_uc5_existing_alert_prevents_duplicate():
    result = assess_lease_deadline(
        _lease(end="2026-11-01"),
        as_of=date(2026, 10, 20),
        reminder_window_days=30,
        alert_already_exists=True,
    )

    assert result.status is LeaseDeadlineStatus.ALERT_ALREADY_EXISTS
    assert not result.alert_eligible


def test_uc5_rejects_invalid_window_and_date_range():
    with pytest.raises(ValueError, match="reminder_window_days"):
        assess_lease_deadline(
            _lease(),
            as_of=date(2026, 10, 20),
            reminder_window_days=-1,
            alert_already_exists=False,
        )

    with pytest.raises(ValueError, match="end_date"):
        assess_lease_deadline(
            _lease(start="2026-12-31", end="2026-01-01"),
            as_of=date(2026, 10, 20),
            reminder_window_days=30,
            alert_already_exists=False,
        )


def test_deduplication_keys_are_stable_and_scenario_specific():
    record = _rent(due="2026-10-01")
    lease = _lease(end="2026-12-31")

    assert rent_reminder_key(record) == "rent-overdue:rent-1:2026-10-01"
    assert (
        lease_deadline_alert_key(lease, reminder_window_days=30)
        == "lease-deadline:lease-1:2026-12-31:30d"
    )
