"""Trusted Maintenance status-event receiver owned by Communication.

Acceptance is durable and replay-safe. Routine factual status updates are
queued from an exact auto-eligible draft; emergency/cancelled events and every
producer-marked review event remain manager-reviewed before delivery.
Maintenance rationale is deliberately excluded from tenant-facing content.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import timezone

from pydantic import ValidationError

from contracts import (
    Draft,
    DraftStatus,
    EventId,
    NotificationEvent,
    NotificationKind,
    RecipientId,
)
from contracts.maintenance import (
    MaintenanceEvent,
    MaintenanceStatus,
    MaintenanceView,
    Priority,
)
from maintenance.outbox import PermanentHandoffError, RetryableHandoffError
from notification import SQLiteNotificationService
from shared import new_id, utcnow


_MESSAGE_TYPE = "maintenance_status"


def canonical_event(payload):
    """Strictly validate and canonicalise the producer event envelope."""

    if not isinstance(payload, dict):
        raise PermanentHandoffError("invalid_event")
    envelope = {"recipient_id": str, "message_type": str, "review_required": bool}
    if set(payload) != set(MaintenanceEvent.model_fields) | set(envelope):
        raise PermanentHandoffError("invalid_event")
    try:
        event = MaintenanceEvent.model_validate(payload)
    except ValidationError:
        raise PermanentHandoffError("invalid_event") from None
    if (
        any(
            not str(value).strip()
            for value in (
                event.event_id,
                event.request_id,
                event.property_id,
                event.actor,
            )
        )
        or event.occurred_at.utcoffset() is None
    ):
        raise PermanentHandoffError("invalid_event")
    for key, expected in envelope.items():
        if type(payload[key]) is not expected:
            raise PermanentHandoffError("invalid_event")
    recipient_id = payload["recipient_id"]
    if not recipient_id.startswith("tenant:") or not recipient_id.removeprefix(
        "tenant:"
    ).strip():
        raise PermanentHandoffError("invalid_recipient")
    if payload["message_type"] != _MESSAGE_TYPE:
        raise PermanentHandoffError("invalid_message_type")

    event = event.model_copy(
        update={"occurred_at": event.occurred_at.astimezone(timezone.utc)}
    )
    normalized = event.model_dump(mode="json")
    normalized.update({key: payload[key] for key in envelope})
    canonical = json.dumps(
        normalized,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return event, recipient_id, payload["review_required"], canonical


class MaintenanceCommunicationReceiver:
    """Durably adapt Maintenance events into Communication-owned drafts."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self._notifications = SQLiteNotificationService(
            connection, commit_changes=False
        )

    def receive(self, payload) -> bool:
        event, recipient_id, review_required, canonical = canonical_event(payload)
        tenant_id = recipient_id.removeprefix("tenant:")
        source_id = f"maintenance-status:{event.event_id}:{recipient_id}"
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            existing = self._connection.execute(
                "SELECT payload FROM communication_maintenance_receipt "
                "WHERE event_id=? AND recipient_id=? AND message_type=?",
                (event.event_id, recipient_id, _MESSAGE_TYPE),
            ).fetchone()
            if existing is not None:
                if existing["payload"] != canonical:
                    raise PermanentHandoffError("event_id_conflict")
                self._connection.commit()
                return True

            producer = self._connection.execute(
                "SELECT document FROM mc_outbox "
                "WHERE event_id=? AND target='communication' AND request_id=?",
                (event.event_id, event.request_id),
            ).fetchone()
            if producer is None:
                raise PermanentHandoffError("source_event_not_found")
            try:
                producer_payload = json.loads(producer["document"])
                producer_canonical = canonical_event(producer_payload)[3]
            except (json.JSONDecodeError, TypeError, PermanentHandoffError):
                raise PermanentHandoffError("invalid_source_event") from None
            if producer_canonical != canonical:
                raise PermanentHandoffError("event_payload_conflict")

            request = self._connection.execute(
                "SELECT tenant_id,property_id,document FROM mc_request WHERE id=?",
                (event.request_id,),
            ).fetchone()
            if request is None:
                raise PermanentHandoffError("request_not_found")
            if (
                request["tenant_id"] != tenant_id
                or request["property_id"] != event.property_id
            ):
                raise PermanentHandoffError("scope_conflict")

            try:
                request_view = MaintenanceView.model_validate_json(
                    request["document"]
                )
            except ValidationError:
                raise PermanentHandoffError("invalid_request_state") from None
            policy_requires_review = (
                request_view.emergency_flag
                or event.priority == Priority.EMERGENCY
                or event.to_status == MaintenanceStatus.CANCELLED
            )
            if policy_requires_review and not review_required:
                raise PermanentHandoffError("review_policy_conflict")

            status = (
                DraftStatus.PENDING_REVIEW
                if review_required
                else DraftStatus.AUTO_ELIGIBLE
            )
            content = _tenant_content(event.to_status)
            now = utcnow()
            draft = Draft(
                draft_id=new_id(),
                request_id=event.request_id,
                source_kind="maintenance_status",
                source_id=source_id,
                content=content,
                status=status,
                created_at=now,
            )
            self._connection.execute(
                "INSERT INTO draft "
                "(draft_id,message_id,request_id,source_kind,source_id,version,"
                "audience,content,tone_hint,review_reason,source_facts,status,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    draft.draft_id,
                    None,
                    draft.request_id,
                    draft.source_kind,
                    draft.source_id,
                    draft.version,
                    draft.audience,
                    draft.content,
                    draft.tone_hint,
                    "maintenance status update requires manager review"
                    if review_required
                    else None,
                    "[]",
                    draft.status.value,
                    _iso(now),
                ),
            )

            notification_event_id = None
            if status == DraftStatus.AUTO_ELIGIBLE:
                notification_event_id = source_id
                self._notifications.publish(
                    NotificationEvent(
                        event_id=EventId(notification_event_id),
                        recipient_id=RecipientId(tenant_id),
                        kind=NotificationKind.MAINTENANCE_STATUS,
                        request_id=event.request_id,
                        payload={
                            "property_id": str(event.property_id),
                            "draft_id": str(draft.draft_id),
                            "draft_version": draft.version,
                            "content": content,
                        },
                        created_at=now,
                    )
                )

            self._connection.execute(
                "INSERT INTO communication_maintenance_receipt "
                "(event_id,recipient_id,message_type,source_id,request_id,tenant_id,"
                "property_id,payload,draft_id,received_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    event.event_id,
                    recipient_id,
                    _MESSAGE_TYPE,
                    source_id,
                    event.request_id,
                    tenant_id,
                    event.property_id,
                    canonical,
                    draft.draft_id,
                    _iso(now),
                ),
            )
            self._connection.execute(
                "INSERT INTO audit "
                "(audit_id,actor,action,subject_type,subject_id,event_id,details,at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    new_id(),
                    "system:communication",
                    "maintenance_status.accepted",
                    "draft",
                    draft.draft_id,
                    event.event_id,
                    json.dumps(
                        {
                            "property_id": str(event.property_id),
                            "request_id": str(event.request_id),
                            "review_required": review_required,
                            "notification_event_id": notification_event_id,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    _iso(now),
                ),
            )
            self._connection.commit()
        except PermanentHandoffError:
            self._connection.rollback()
            raise
        except sqlite3.OperationalError as exc:
            self._connection.rollback()
            if getattr(exc, "sqlite_errorcode", None) in (
                sqlite3.SQLITE_BUSY,
                sqlite3.SQLITE_LOCKED,
            ):
                raise RetryableHandoffError("communication_busy") from None
            raise
        except Exception:
            self._connection.rollback()
            raise
        return True


def _tenant_content(status: MaintenanceStatus) -> str:
    messages = {
        MaintenanceStatus.SUBMITTED: "Your maintenance request has been received.",
        MaintenanceStatus.ASSESSED: "Your maintenance request has been assessed.",
        MaintenanceStatus.UNDER_REVIEW: "Your maintenance request is under review.",
        MaintenanceStatus.APPROVED: "Your maintenance request has been approved.",
        MaintenanceStatus.IN_PROGRESS: "Work on your maintenance request is in progress.",
        MaintenanceStatus.AWAITING_CONFIRMATION: (
            "Work on your maintenance request is awaiting confirmation."
        ),
        MaintenanceStatus.CLOSED: "Your maintenance request has been closed.",
        MaintenanceStatus.CANCELLED: "Your maintenance request has been cancelled.",
    }
    return messages[status]


def _iso(value) -> str:
    return value.isoformat().replace("+00:00", "Z")
