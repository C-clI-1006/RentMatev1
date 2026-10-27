"""Manager review workflow for tenant-facing drafts.

Review decisions are immutable and bound to one exact draft version. This
module does not contact a delivery transport. It atomically records the
decision and queues the exact approved content for the later single-owner
notification dispatcher.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from pydantic import BaseModel, Field

from contracts import (
    Draft,
    DraftStatus,
    EventId,
    FactReference,
    MessageId,
    NotificationEvent,
    NotificationKind,
    RecipientId,
    ReviewDecisionRecord,
)
from notification import SQLiteNotificationService
from contracts.drafting import ReviewDecision
from permissions import (
    SQLiteManagerRepository,
    manager_id_from_auth,
    require_manager_property_access,
)
from shared import AuthContext, new_id, utcnow


class ReviewNotFoundError(LookupError):
    pass


class ReviewConflictError(RuntimeError):
    pass


class InvalidReviewDecisionError(ValueError):
    pass


class ReviewQueueItem(BaseModel):
    """Authorised manager view of one pending draft."""

    draft: Draft
    property_id: str
    original_message: str | None = None
    reason: str
    source_facts: list[FactReference] = Field(default_factory=list)


class ReviewService:
    def __init__(self, connection: sqlite3.Connection, auth: AuthContext) -> None:
        self._connection = connection
        self.auth = auth
        self._notifications = SQLiteNotificationService(
            connection, commit_changes=False
        )

    def _require_scope(self) -> str:
        manager_id = manager_id_from_auth(self.auth)
        require_manager_property_access(
            manager_id,
            self.auth.property_id,
            SQLiteManagerRepository(self._connection),
        )
        return str(manager_id)

    def list_pending(self) -> list[ReviewQueueItem]:
        self._require_scope()
        rows = self._connection.execute(
            _DRAFT_SELECT
            + " WHERE d.status = 'pending_review' AND "
            + _PROPERTY_EXPRESSION
            + " = ? ORDER BY d.created_at, d.draft_id",
            (self.auth.property_id,),
        ).fetchall()
        return [self._queue_item(row) for row in rows]

    def get_pending(self, draft_id: str) -> ReviewQueueItem:
        self._require_scope()
        row = self._connection.execute(
            _DRAFT_SELECT
            + " WHERE d.draft_id = ? AND d.status = 'pending_review' AND "
            + _PROPERTY_EXPRESSION
            + " = ?",
            (draft_id, self.auth.property_id),
        ).fetchone()
        if row is None:
            raise ReviewNotFoundError("pending review draft not found")
        return self._queue_item(row)

    def decide(
        self,
        draft_id: str,
        *,
        expected_version: int,
        decision: ReviewDecision,
        final_content: str | None = None,
        reason: str | None = None,
    ) -> ReviewDecisionRecord:
        manager_id = self._require_scope()
        clean_reason = reason.strip() if reason else None
        clean_content = final_content.strip() if final_content is not None else None
        if decision is ReviewDecision.REJECTED:
            if not clean_reason:
                raise InvalidReviewDecisionError("a rejection reason is required")
            if clean_content is not None:
                raise InvalidReviewDecisionError("rejected drafts cannot have final content")

        # Serialize competing decisions before checking the current status.
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            row = self._connection.execute(
                _DRAFT_SELECT
                + " WHERE d.draft_id = ? AND "
                + _PROPERTY_EXPRESSION
                + " = ?",
                (draft_id, self.auth.property_id),
            ).fetchone()
            if row is None:
                raise ReviewNotFoundError("draft not found")
            if row["status"] != DraftStatus.PENDING_REVIEW.value:
                raise ReviewConflictError("draft is no longer pending review")
            if row["version"] != expected_version:
                raise ReviewConflictError("draft version is stale")

            if decision is ReviewDecision.APPROVED:
                if final_content is not None and not clean_content:
                    raise InvalidReviewDecisionError("approved content cannot be empty")
                clean_content = clean_content or row["content"].strip()
                if not clean_content:
                    raise InvalidReviewDecisionError("approved content cannot be empty")

            now = utcnow()
            record = ReviewDecisionRecord(
                review_id=new_id(),
                draft_id=draft_id,
                draft_version=expected_version,
                decision=decision,
                manager_id=manager_id,
                final_content=clean_content,
                reason=clean_reason,
                decided_at=now,
            )
            self._connection.execute(
                "INSERT INTO review (review_id,draft_id,draft_version,decision,"
                "manager_id,final_content,reason,decided_at) VALUES (?,?,?,?,?,?,?,?)",
                (
                    record.review_id,
                    record.draft_id,
                    record.draft_version,
                    record.decision.value,
                    record.manager_id,
                    record.final_content,
                    record.reason,
                    _iso(now),
                ),
            )
            updated = self._connection.execute(
                "UPDATE draft SET status=? WHERE draft_id=? AND version=? "
                "AND status='pending_review'",
                (decision.value, draft_id, expected_version),
            )
            if updated.rowcount != 1:
                raise ReviewConflictError("draft changed while it was being reviewed")
            notification_event_id = None
            if decision is ReviewDecision.APPROVED:
                if not row["recipient_id"]:
                    raise ReviewConflictError("draft recipient cannot be resolved")
                notification_event_id = f"draft:{draft_id}:v{expected_version}"
                if row["source_kind"] == "maintenance_status":
                    kind = NotificationKind.MAINTENANCE_STATUS
                elif row["source_kind"] in ("rent_reminder", "lease_deadline") or row[
                    "message_type"
                ] == "lease_rent":
                    kind = NotificationKind.LEASE_RENT_REPLY
                else:
                    kind = NotificationKind.OTHER
                self._notifications.publish(
                    NotificationEvent(
                        event_id=EventId(notification_event_id),
                        recipient_id=RecipientId(row["recipient_id"]),
                        kind=kind,
                        request_id=row["request_id"],
                        message_id=(
                            MessageId(row["message_id"])
                            if row["message_id"] is not None
                            else None
                        ),
                        payload={
                            "property_id": row["property_id"],
                            "draft_id": draft_id,
                            "draft_version": expected_version,
                            "content": clean_content,
                        },
                        created_at=now,
                    )
                )
            self._connection.execute(
                "INSERT INTO audit (audit_id,actor,action,subject_type,subject_id,"
                "details,at) VALUES (?,?,?,?,?,?,?)",
                (
                    new_id(),
                    "manager:" + manager_id,
                    "review." + decision.value,
                    "draft",
                    draft_id,
                    json.dumps(
                        {
                            "review_id": record.review_id,
                            "draft_version": expected_version,
                            "notification_event_id": notification_event_id,
                        },
                        ensure_ascii=False,
                    ),
                    _iso(now),
                ),
            )
            self._connection.commit()
            return record
        except Exception:
            self._connection.rollback()
            raise

    @staticmethod
    def _queue_item(row: sqlite3.Row) -> ReviewQueueItem:
        refs = [
            FactReference.model_validate(value)
            for value in json.loads(row["source_facts"] or "[]")
        ]
        draft = Draft(
            draft_id=row["draft_id"],
            message_id=row["message_id"],
            request_id=row["request_id"],
            source_kind=row["source_kind"],
            source_id=row["source_id"],
            version=row["version"],
            audience=row["audience"],
            content=row["content"],
            tone_hint=row["tone_hint"],
            source_facts=refs,
            status=DraftStatus(row["status"]),
            created_at=_datetime(row["created_at"]),
        )
        return ReviewQueueItem(
            draft=draft,
            property_id=row["property_id"],
            original_message=row["original_message"],
            reason=row["review_reason"] or "manager review required",
            source_facts=refs,
        )


_PROPERTY_EXPRESSION = (
    "COALESCE(m.property_id,cmr.property_id,mr.property_id,rr.property_id,l.property_id)"
)
_DRAFT_SELECT = """
SELECT d.*, m.content AS original_message, m.message_type AS message_type,
       COALESCE(m.tenant_id,cmr.tenant_id,mr.tenant_id,l.tenant_id) AS recipient_id,
       COALESCE(m.property_id,cmr.property_id,mr.property_id,rr.property_id,l.property_id) AS property_id
FROM draft d
LEFT JOIN message m ON m.message_id=d.message_id
LEFT JOIN communication_maintenance_receipt cmr
  ON d.source_kind='maintenance_status' AND d.source_id=cmr.source_id
LEFT JOIN maintenance_request mr ON mr.request_id=d.request_id
LEFT JOIN rent_record rr
  ON d.source_kind='rent_reminder'
 AND d.source_id=('rent-overdue:' || rr.rent_record_id || ':' || rr.due_date)
LEFT JOIN lease l
  ON (d.source_kind='lease_deadline'
      AND d.source_id=('lease-deadline:' || l.lease_id || ':' || l.end_date))
  OR (d.source_kind='rent_reminder' AND l.lease_id=rr.lease_id)
"""


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
