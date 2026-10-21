"""Scheduled UC4/UC5 orchestration over the lease/rent repository."""

from datetime import date, datetime

from .models import LeaseDeadlineAlertCandidate, RentReminderCandidate
from .repository import SQLiteLeaseRentRepository
from .service import prepare_deadline_alert_batch, prepare_overdue_rent_batch


def run_overdue_rent_check(
    repository: SQLiteLeaseRentRepository, *, as_of: date
) -> tuple[RentReminderCandidate, ...]:
    """Run UC4 and return traceable candidates for reviewable drafts."""

    records = repository.rent_records_for_check(as_of=as_of)
    return prepare_overdue_rent_batch(records, as_of=as_of)


def run_lease_deadline_check(
    repository: SQLiteLeaseRentRepository,
    *,
    as_of: date,
    reminder_window_days: int,
    created_at: datetime,
) -> tuple[LeaseDeadlineAlertCandidate, ...]:
    """Run UC5 and durably record only newly prepared deadline alerts."""

    candidates = prepare_deadline_alert_batch(
        repository.leases_for_deadline_check(),
        as_of=as_of,
        reminder_window_days=reminder_window_days,
        existing_alert_keys=repository.existing_deadline_alert_keys(),
    )
    return tuple(
        candidate
        for candidate in candidates
        if repository.record_deadline_alert(candidate, created_at=created_at)
    )
