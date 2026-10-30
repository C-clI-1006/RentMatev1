"""UC4/UC5 scheduled candidates -> manager-review drafts.

The deterministic domain rules decide eligibility first.  This adapter only
persists eligible candidates, preserves their stable source keys and always
marks the resulting tenant-facing text ``pending_review``.  It never sends a
message and never treats draft creation as manager approval.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from contracts import Draft, DraftStatus, FactReference
from shared import new_id
from shared.sqlite import init_connection

from .models import LeaseDeadlineAlertCandidate, RentReminderCandidate
from .repository import LeaseRentDataError, SQLiteLeaseRentRepository
from .service import prepare_deadline_alert_batch, prepare_overdue_rent_batch


_DEFAULT_DB_PATH = Path(__file__).resolve().parents[1] / "rentmate.db"


class SQLiteScheduledDraftWriter:
    """Cross-module adapter for system-generated, review-required drafts."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        repository: SQLiteLeaseRentRepository,
    ) -> None:
        self._connection = connection
        self._repository = repository

    def write_rent_reminder(
        self, candidate: RentReminderCandidate, *, created_at: datetime
    ) -> bool:
        """Persist one UC4 reminder draft; return True only when newly made."""

        scope = self._repository.resolve_source_scope(
            source_type="rent_record",
            source_id=candidate.rent_record_id,
        )
        if scope is None:
            raise LeaseRentDataError(
                f"rent record {candidate.rent_record_id} no longer exists"
            )
        reference = FactReference(
            source_type="rent_record",
            source_id=candidate.rent_record_id,
            snippet=(
                f"due_date={candidate.due_date.isoformat()}; "
                f"outstanding_amount={_decimal_text(candidate.outstanding_amount)}; "
                f"overdue_days={candidate.overdue_days}"
            ),
        )
        draft = Draft(
            draft_id=new_id(),
            source_kind="rent_reminder",
            source_id=candidate.candidate_key,
            content=(
                "Rent reminder: "
                f"{_decimal_text(candidate.outstanding_amount)} remains outstanding "
                f"for rent due on {candidate.due_date.isoformat()} "
                f"({candidate.overdue_days} days overdue as of "
                f"{candidate.prepared_on.isoformat()})."
            ),
            source_facts=[reference],
            status=DraftStatus.PENDING_REVIEW,
            created_at=created_at,
        )
        return self._insert_draft(
            draft,
            review_reason="scheduled overdue-rent reminder (UC4)",
            tenant_id=scope.tenant_id,
            property_id=scope.property_id,
        )

    def write_lease_deadline(
        self, candidate: LeaseDeadlineAlertCandidate, *, created_at: datetime
    ) -> tuple[bool, bool]:
        """Persist the UC5 detection record and draft.

        Returns ``(draft_created, alert_record_created)``.  The two writes run
        inside the caller's transaction, so an interrupted first run can be
        retried without leaving a duplicate or permanently losing the draft.
        """

        scope = self._repository.resolve_source_scope(
            source_type="lease",
            source_id=candidate.lease_id,
        )
        if scope is None:
            raise LeaseRentDataError(f"lease {candidate.lease_id} no longer exists")
        alert_created = self._ensure_deadline_alert(
            candidate,
            tenant_id=scope.tenant_id,
            property_id=scope.property_id,
            created_at=created_at,
        )
        reference = FactReference(
            source_type="lease",
            source_id=candidate.lease_id,
            snippet=(
                f"end_date={candidate.end_date.isoformat()}; "
                f"remaining_days={candidate.remaining_days}"
            ),
        )
        draft = Draft(
            draft_id=new_id(),
            source_kind="lease_deadline",
            source_id=candidate.candidate_key,
            content=(
                f"Lease deadline alert: your lease ends on "
                f"{candidate.end_date.isoformat()} "
                f"({candidate.remaining_days} days remaining as of "
                f"{candidate.prepared_on.isoformat()}). Please contact your "
                "property manager to discuss next steps."
            ),
            source_facts=[reference],
            status=DraftStatus.PENDING_REVIEW,
            created_at=created_at,
        )
        draft_created = self._insert_draft(
            draft,
            review_reason="scheduled lease-deadline alert (UC5)",
            tenant_id=scope.tenant_id,
            property_id=scope.property_id,
            remaining_days=candidate.remaining_days,
        )
        return draft_created, alert_created

    def _insert_draft(
        self,
        draft: Draft,
        *,
        review_reason: str,
        tenant_id: str,
        property_id: str,
        remaining_days: int | None = None,
    ) -> bool:
        cursor = self._connection.execute(
            "INSERT OR IGNORE INTO draft "
            "(draft_id,message_id,request_id,source_kind,source_id,version,"
            "audience,content,tone_hint,review_reason,source_facts,status,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                draft.draft_id,
                None,
                None,
                draft.source_kind,
                draft.source_id,
                draft.version,
                draft.audience,
                draft.content,
                draft.tone_hint,
                review_reason,
                json.dumps(
                    [value.model_dump(mode="json") for value in draft.source_facts],
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                draft.status.value,
                _utc_text(draft.created_at),
            ),
        )
        if cursor.rowcount == 0:
            existing = self._connection.execute(
                "SELECT draft_id,source_kind FROM draft WHERE source_id = ?",
                (draft.source_id,),
            ).fetchone()
            if existing is None or existing["source_kind"] != draft.source_kind:
                raise LeaseRentDataError(
                    f"conflicting draft origin for source {draft.source_id}"
                )
            return False

        reference = draft.source_facts[0]
        basis = {
            "facts": self._repository.reminder_facts(
                source_type=reference.source_type, source_id=str(reference.source_id)),
            "remaining_days": remaining_days,
        }
        self._connection.execute("INSERT INTO reminder_basis(draft_id,basis) VALUES (?,?)",
                                 (draft.draft_id, json.dumps(basis, sort_keys=True)))
        self._connection.execute(
            "INSERT INTO audit "
            "(audit_id,actor,action,subject_type,subject_id,event_id,details,at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                new_id(),
                "system:scheduler",
                "draft.prepared",
                "draft",
                draft.draft_id,
                draft.source_id,
                json.dumps(
                    {
                        "property_id": property_id,
                        "source_kind": draft.source_kind,
                        "source_id": draft.source_id,
                        "tenant_id": tenant_id,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                _utc_text(draft.created_at),
            ),
        )
        return True

    def _ensure_deadline_alert(
        self,
        candidate: LeaseDeadlineAlertCandidate,
        *,
        tenant_id: str,
        property_id: str,
        created_at: datetime,
    ) -> bool:
        cursor = self._connection.execute(
            "INSERT OR IGNORE INTO lease_deadline_alert "
            "(alert_id,candidate_key,lease_id,tenant_id,property_id,end_date,"
            "remaining_days,prepared_on,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                new_id(),
                candidate.candidate_key,
                candidate.lease_id,
                tenant_id,
                property_id,
                candidate.end_date.isoformat(),
                candidate.remaining_days,
                candidate.prepared_on.isoformat(),
                _utc_text(created_at),
            ),
        )
        if cursor.rowcount == 1:
            return True

        existing = self._connection.execute(
            "SELECT lease_id,tenant_id,property_id,end_date "
            "FROM lease_deadline_alert WHERE candidate_key = ?",
            (candidate.candidate_key,),
        ).fetchone()
        expected = (
            candidate.lease_id,
            tenant_id,
            property_id,
            candidate.end_date.isoformat(),
        )
        if existing is None or tuple(existing) != expected:
            raise LeaseRentDataError(
                f"conflicting lease deadline alert for {candidate.candidate_key}"
            )
        return False


def run_due_alerts(
    *,
    now: datetime,
    db_path: str | Path | None = None,
    reminder_window_days: int = 30,
) -> dict[str, int | str]:
    """Run UC4 and UC5, then create idempotent manager-review drafts."""

    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    if reminder_window_days < 0:
        raise ValueError("reminder_window_days must be non-negative")

    now_utc = now.astimezone(timezone.utc)
    database = db_path or os.getenv("DATABASE_PATH") or _DEFAULT_DB_PATH
    connection = init_connection(database)
    try:
        repository = SQLiteLeaseRentRepository(connection)
        writer = SQLiteScheduledDraftWriter(connection, repository)
        connection.execute("BEGIN IMMEDIATE")
        try:
            rent_candidates = prepare_overdue_rent_batch(
                repository.rent_records_for_check(as_of=now_utc.date()),
                as_of=now_utc.date(),
            )
            # Do not filter by the detection table here: INSERT OR IGNORE on
            # both the alert and draft lets an interrupted earlier run repair
            # whichever half was missing.
            lease_candidates = prepare_deadline_alert_batch(
                repository.leases_for_deadline_check(),
                as_of=now_utc.date(),
                reminder_window_days=reminder_window_days,
            )

            drafts_created = 0
            drafts_reused = 0
            deadline_alerts_created = 0
            for candidate in rent_candidates:
                if writer.write_rent_reminder(candidate, created_at=now_utc):
                    drafts_created += 1
                else:
                    drafts_reused += 1
            for candidate in lease_candidates:
                draft_created, alert_created = writer.write_lease_deadline(
                    candidate,
                    created_at=now_utc,
                )
                drafts_created += int(draft_created)
                drafts_reused += int(not draft_created)
                deadline_alerts_created += int(alert_created)
            connection.commit()
        except Exception:
            connection.rollback()
            raise

        return {
            "as_of": now_utc.date().isoformat(),
            "rent_candidates": len(rent_candidates),
            "lease_candidates": len(lease_candidates),
            "drafts_created": drafts_created,
            "drafts_reused": drafts_reused,
            "deadline_alerts_created": deadline_alerts_created,
        }
    finally:
        connection.close()


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"
