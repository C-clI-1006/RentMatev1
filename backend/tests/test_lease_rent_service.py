"""Tests for dependency-free UC4/UC5 preparation flows."""

from datetime import date
from decimal import Decimal

import pytest

from lease_rent import (
    LeaseDeadlineInput,
    RentRecordInput,
    prepare_deadline_alert,
    prepare_deadline_alert_batch,
    prepare_overdue_rent_batch,
    prepare_rent_reminder,
)


TODAY = date(2026, 10, 20)


def _rent(
    record_id: str,
    *,
    due_date: date,
    amount_due: str = "1000",
    amount_paid: str = "0",
) -> RentRecordInput:
    return RentRecordInput(
        rent_record_id=record_id,
        due_date=due_date,
        amount_due=Decimal(amount_due),
        amount_paid=Decimal(amount_paid),
    )


def _lease(
    lease_id: str,
    *,
    start_date: date = date(2026, 1, 1),
    end_date: date,
) -> LeaseDeadlineInput:
    return LeaseDeadlineInput(
        lease_id=lease_id,
        start_date=start_date,
        end_date=end_date,
    )


def test_uc4_prepares_traceable_candidate_for_overdue_record():
    candidate = prepare_rent_reminder(
        _rent("rent-7", due_date=date(2026, 10, 1), amount_paid="250"),
        as_of=TODAY,
    )

    assert candidate is not None
    assert candidate.candidate_key == "rent-overdue:rent-7:2026-10-01"
    assert candidate.rent_record_id == "rent-7"
    assert candidate.outstanding_amount == Decimal("750")
    assert candidate.overdue_days == 19
    assert candidate.prepared_on == TODAY


@pytest.mark.parametrize(
    "record",
    [
        _rent("paid", due_date=date(2026, 10, 1), amount_paid="1000"),
        _rent("not-due", due_date=date(2026, 10, 21)),
    ],
)
def test_uc4_does_not_prepare_ineligible_reminder(record):
    assert prepare_rent_reminder(record, as_of=TODAY) is None


def test_uc4_batch_filters_and_deduplicates_identical_records():
    overdue = _rent("overdue", due_date=date(2026, 10, 1))
    result = prepare_overdue_rent_batch(
        [
            _rent("paid", due_date=date(2026, 10, 1), amount_paid="1000"),
            overdue,
            overdue,
        ],
        as_of=TODAY,
    )

    assert len(result) == 1
    assert result[0].rent_record_id == "overdue"


def test_uc4_batch_rejects_conflicting_duplicate_records():
    with pytest.raises(ValueError, match="conflicting duplicate rent record"):
        prepare_overdue_rent_batch(
            [
                _rent("rent-1", due_date=date(2026, 10, 1), amount_paid="0"),
                _rent("rent-1", due_date=date(2026, 10, 1), amount_paid="500"),
            ],
            as_of=TODAY,
        )


def test_uc5_prepares_traceable_candidate_when_all_guards_pass():
    candidate = prepare_deadline_alert(
        _lease("lease-7", end_date=date(2026, 11, 19)),
        as_of=TODAY,
        reminder_window_days=30,
    )

    assert candidate is not None
    assert candidate.candidate_key == "lease-deadline:lease-7:2026-11-19"
    assert candidate.lease_id == "lease-7"
    assert candidate.remaining_days == 30
    assert candidate.prepared_on == TODAY


def test_uc5_existing_alert_key_prevents_duplicate_candidate():
    lease = _lease("lease-7", end_date=date(2026, 11, 19))

    assert (
        prepare_deadline_alert(
            lease,
            as_of=TODAY,
            reminder_window_days=30,
            existing_alert_keys={"lease-deadline:lease-7:2026-11-19"},
        )
        is None
    )


def test_uc5_batch_keeps_only_eligible_lease_and_is_repeatable():
    leases = [
        _lease("eligible", end_date=date(2026, 11, 1)),
        _lease("outside", end_date=date(2026, 12, 31)),
        _lease("expired", end_date=date(2026, 10, 19)),
    ]

    first = prepare_deadline_alert_batch(
        leases,
        as_of=TODAY,
        reminder_window_days=30,
    )
    second = prepare_deadline_alert_batch(
        reversed(leases),
        as_of=TODAY,
        reminder_window_days=30,
    )

    assert first == second
    assert len(first) == 1
    assert first[0].lease_id == "eligible"


def test_uc5_batch_rejects_conflicting_duplicate_leases():
    with pytest.raises(ValueError, match="conflicting duplicate lease"):
        prepare_deadline_alert_batch(
            [
                _lease("lease-1", end_date=date(2026, 11, 1)),
                _lease(
                    "lease-1",
                    start_date=date(2026, 2, 1),
                    end_date=date(2026, 11, 1),
                ),
            ],
            as_of=TODAY,
            reminder_window_days=30,
        )
