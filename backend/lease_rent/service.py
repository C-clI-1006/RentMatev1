"""UC4/UC5 preparation service.

The preparation functions remain dependency-free and stop at traceable
candidates. ``run_due_alerts`` is the public scheduled orchestration entry and
delegates persistence to the cross-module adapter in ``due_alerts.py``.
"""

from collections.abc import Collection, Iterable
from datetime import date, datetime
from pathlib import Path
from typing import TypeVar

from .models import (
    LeaseDeadlineAlertCandidate,
    LeaseDeadlineInput,
    RentRecordInput,
    RentReminderCandidate,
)
from .rules import (
    assess_lease_deadline,
    assess_rent,
    lease_deadline_alert_key,
    rent_reminder_key,
)


T = TypeVar("T")


def prepare_rent_reminder(
    record: RentRecordInput, *, as_of: date
) -> RentReminderCandidate | None:
    """Prepare a UC4 candidate only when the record is genuinely overdue."""

    assessment = assess_rent(record, as_of=as_of)
    if not assessment.reminder_eligible:
        return None

    return RentReminderCandidate(
        candidate_key=rent_reminder_key(record),
        rent_record_id=record.rent_record_id,
        due_date=record.due_date,
        outstanding_amount=assessment.outstanding_amount,
        overdue_days=assessment.overdue_days,
        prepared_on=as_of,
    )


def prepare_deadline_alert(
    lease: LeaseDeadlineInput,
    *,
    as_of: date,
    reminder_window_days: int,
    existing_alert_keys: Collection[str] = (),
) -> LeaseDeadlineAlertCandidate | None:
    """Prepare a UC5 candidate only when all eligibility guards pass."""

    candidate_key = lease_deadline_alert_key(lease)
    assessment = assess_lease_deadline(
        lease,
        as_of=as_of,
        reminder_window_days=reminder_window_days,
        alert_already_exists=candidate_key in existing_alert_keys,
    )
    if not assessment.alert_eligible:
        return None

    return LeaseDeadlineAlertCandidate(
        candidate_key=candidate_key,
        lease_id=lease.lease_id,
        end_date=lease.end_date,
        remaining_days=assessment.remaining_days,
        prepared_on=as_of,
    )


def prepare_overdue_rent_batch(
    records: Iterable[RentRecordInput], *, as_of: date
) -> tuple[RentReminderCandidate, ...]:
    """Prepare a deterministic UC4 batch and reject conflicting duplicates."""

    seen: dict[str, RentRecordInput] = {}
    candidates: dict[str, RentReminderCandidate] = {}
    for record in records:
        key = rent_reminder_key(record)
        _record_or_reject_conflict(seen, key, record, item_name="rent record")
        candidate = prepare_rent_reminder(record, as_of=as_of)
        if candidate is not None:
            candidates[key] = candidate

    return tuple(candidates[key] for key in sorted(candidates))


def prepare_deadline_alert_batch(
    leases: Iterable[LeaseDeadlineInput],
    *,
    as_of: date,
    reminder_window_days: int,
    existing_alert_keys: Collection[str] = (),
) -> tuple[LeaseDeadlineAlertCandidate, ...]:
    """Prepare a deterministic UC5 batch and reject conflicting duplicates."""

    seen: dict[str, LeaseDeadlineInput] = {}
    candidates: dict[str, LeaseDeadlineAlertCandidate] = {}
    for lease in leases:
        key = lease_deadline_alert_key(lease)
        _record_or_reject_conflict(seen, key, lease, item_name="lease")
        candidate = prepare_deadline_alert(
            lease,
            as_of=as_of,
            reminder_window_days=reminder_window_days,
            existing_alert_keys=existing_alert_keys,
        )
        if candidate is not None:
            candidates[key] = candidate

    return tuple(candidates[key] for key in sorted(candidates))


def _record_or_reject_conflict(
    seen: dict[str, T], key: str, item: T, *, item_name: str
) -> None:
    previous = seen.get(key)
    if previous is not None and previous != item:
        raise ValueError(f"conflicting duplicate {item_name} for key {key}")
    seen[key] = item


def run_due_alerts(
    *,
    now: datetime,
    db_path: str | Path | None = None,
    reminder_window_days: int = 30,
) -> dict[str, int | str]:
    """Create idempotent UC4/UC5 manager-review drafts.

    The deferred import keeps the pure rule/preparation layer usable without
    loading SQLite and the shared drafting contract.
    """

    from .due_alerts import run_due_alerts as _run_due_alerts

    return _run_due_alerts(
        now=now,
        db_path=db_path,
        reminder_window_days=reminder_window_days,
    )
