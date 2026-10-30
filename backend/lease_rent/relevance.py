"""Dispatch-time relevance of immutable scheduled reminder drafts."""

import json
import sqlite3
from datetime import date
from decimal import Decimal

from .models import LeaseDeadlineInput, RentRecordInput
from .repository import SQLiteLeaseRentRepository
from .rules import assess_lease_deadline, assess_rent, rent_reminder_key, lease_deadline_alert_key


class ObsoleteReminder(ValueError):
    """A definite suppression; a new review is needed for changed facts."""


def validate_reminder(connection, draft_id, *, tenant_id, property_id, as_of):
    """Called inside the dispatch write transaction; never edits approved text."""
    draft = connection.execute('SELECT source_kind,source_id FROM draft WHERE draft_id=?', (draft_id,)).fetchone()
    if draft is None or draft['source_kind'] not in ('rent_reminder', 'lease_deadline'):
        return
    saved = connection.execute('SELECT basis FROM reminder_basis WHERE draft_id=?', (draft_id,)).fetchone()
    if saved is None:
        raise ObsoleteReminder('reminder_basis_unverifiable')
    try:
        basis = json.loads(saved['basis'])
        old = basis['facts']
        expected_type = 'rent_record' if draft['source_kind'] == 'rent_reminder' else 'lease'
        if old['source_type'] != expected_type:
            raise ObsoleteReminder('reminder_basis_unverifiable')
        current = SQLiteLeaseRentRepository(connection).reminder_facts(
            source_type=expected_type, source_id=old['source_id'])
        if current['tenant_id'] != tenant_id or current['property_id'] != property_id:
            raise ObsoleteReminder('reminder_scope_changed')
        if current != old:
            raise ObsoleteReminder('reminder_facts_changed')
        start = date.fromisoformat(current['start_date'])
        end = date.fromisoformat(current['end_date']) if current['end_date'] else None
        if as_of < start or (end is not None and as_of > end):
            raise ObsoleteReminder('reminder_lease_inactive')
        if expected_type == 'rent_record':
            record = RentRecordInput(current['source_id'], date.fromisoformat(current['due_date']),
                                     Decimal(current['amount_due']), Decimal(current['amount_paid']))
            if draft['source_id'] != rent_reminder_key(record):
                raise ObsoleteReminder('reminder_basis_unverifiable')
            if not assess_rent(record, as_of=as_of).reminder_eligible:
                raise ObsoleteReminder('reminder_no_longer_overdue')
        else:
            lease = LeaseDeadlineInput(current['source_id'], start, end)
            if draft['source_id'] != lease_deadline_alert_key(lease):
                raise ObsoleteReminder('reminder_basis_unverifiable')
            if not assess_lease_deadline(lease, as_of=as_of,
                    reminder_window_days=basis['remaining_days'], alert_already_exists=False).alert_eligible:
                raise ObsoleteReminder('reminder_deadline_ineligible')
    except ObsoleteReminder:
        raise
    except sqlite3.OperationalError:
        # Let the existing dispatcher retain an unverifiable transient read as
        # pending_retry, without invoking the delivery adapter.
        raise
    except (ValueError, TypeError, KeyError, IndexError, AttributeError, ArithmeticError):
        raise ObsoleteReminder('reminder_facts_unverifiable') from None
