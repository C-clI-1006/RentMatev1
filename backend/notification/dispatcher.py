"""Single-owner delivery dispatcher for the durable notification outbox."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from typing import Protocol

from contracts import DeliveryResult, NotificationKind, NotificationStatus
from shared import new_id, utcnow

from .service import NotificationNotFoundError


class DeliveryAdapter(Protocol):
    """External delivery boundary.

    Implementations must deduplicate on ``event_id`` and ``recipient_id`` so a
    crash after the external send but before the local commit cannot duplicate
    a recipient-facing message.
    """

    def deliver(
        self,
        *,
        event_id: str,
        recipient_id: str,
        content: str,
    ) -> None: ...


class RetryableDeliveryError(RuntimeError):
    pass


class PermanentDeliveryError(RuntimeError):
    pass


class SQLiteTenantInboxAdapter:
    """Durable, idempotent prototype transport visible to the tenant API."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def deliver(
        self,
        *,
        event_id: str,
        recipient_id: str,
        content: str,
    ) -> None:
        event = self._connection.execute(
            "SELECT kind,payload FROM notification_event "
            "WHERE event_id=? AND recipient_id=?",
            (event_id, recipient_id),
        ).fetchone()
        if event is None:
            raise PermanentDeliveryError("notification event not found")
        if NotificationKind(event["kind"]) is NotificationKind.MANAGER_ALERT:
            raise PermanentDeliveryError(
                "manager alerts require the manager delivery adapter"
            )
        try:
            property_id = json.loads(event["payload"])["property_id"]
        except (json.JSONDecodeError, KeyError, TypeError) as error:
            raise PermanentDeliveryError("delivery property is missing") from error
        now = _iso(utcnow())
        inserted = self._connection.execute(
            "INSERT OR IGNORE INTO tenant_inbox "
            "(event_id,recipient_id,property_id,content,delivered_at) "
            "VALUES (?,?,?,?,?)",
            (event_id, recipient_id, property_id, content, now),
        )
        if inserted.rowcount == 0:
            existing = self._connection.execute(
                "SELECT property_id,content FROM tenant_inbox "
                "WHERE event_id=? AND recipient_id=?",
                (event_id, recipient_id),
            ).fetchone()
            if existing is None or tuple(existing) != (property_id, content):
                raise PermanentDeliveryError(
                    "tenant inbox key was reused with different content"
                )


class SQLiteManagerInboxAdapter:
    """Durable, idempotent prototype transport for authorised managers."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def deliver(
        self,
        *,
        event_id: str,
        recipient_id: str,
        content: str,
    ) -> None:
        event = self._connection.execute(
            "SELECT kind,payload FROM notification_event "
            "WHERE event_id=? AND recipient_id=?",
            (event_id, recipient_id),
        ).fetchone()
        if event is None:
            raise PermanentDeliveryError("notification event not found")
        if NotificationKind(event["kind"]) is not NotificationKind.MANAGER_ALERT:
            raise PermanentDeliveryError(
                "tenant notifications require the tenant delivery adapter"
            )
        try:
            property_id = json.loads(event["payload"])["property_id"]
        except (json.JSONDecodeError, KeyError, TypeError) as error:
            raise PermanentDeliveryError("delivery property is missing") from error
        now = _iso(utcnow())
        inserted = self._connection.execute(
            "INSERT OR IGNORE INTO manager_inbox "
            "(event_id,recipient_id,property_id,content,delivered_at) "
            "VALUES (?,?,?,?,?)",
            (event_id, recipient_id, property_id, content, now),
        )
        if inserted.rowcount == 0:
            existing = self._connection.execute(
                "SELECT property_id,content FROM manager_inbox "
                "WHERE event_id=? AND recipient_id=?",
                (event_id, recipient_id),
            ).fetchone()
            if existing is None or tuple(existing) != (property_id, content):
                raise PermanentDeliveryError(
                    "manager inbox key was reused with different content"
                )


class SQLiteNotificationInboxAdapter:
    """Route each event to its role-specific durable prototype transport."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self._tenant = SQLiteTenantInboxAdapter(connection)
        self._manager = SQLiteManagerInboxAdapter(connection)

    def deliver(
        self,
        *,
        event_id: str,
        recipient_id: str,
        content: str,
    ) -> None:
        event = self._connection.execute(
            "SELECT kind FROM notification_event "
            "WHERE event_id=? AND recipient_id=?",
            (event_id, recipient_id),
        ).fetchone()
        if event is None:
            raise PermanentDeliveryError("notification event not found")
        try:
            kind = NotificationKind(event["kind"])
        except ValueError as error:
            raise PermanentDeliveryError("invalid notification kind") from error
        adapter = (
            self._manager
            if kind is NotificationKind.MANAGER_ALERT
            else self._tenant
        )
        adapter.deliver(
            event_id=event_id,
            recipient_id=recipient_id,
            content=content,
        )


def dispatch_one(
    connection: sqlite3.Connection,
    event_id: str,
    recipient_id: str,
    adapter: DeliveryAdapter,
) -> DeliveryResult:
    """Attempt one accepted/retryable event and persist the truthful result."""

    connection.execute("BEGIN IMMEDIATE")
    try:
        row = connection.execute(
            "SELECT * FROM notification_event WHERE event_id=? AND recipient_id=?",
            (event_id, recipient_id),
        ).fetchone()
        if row is None:
            raise NotificationNotFoundError("notification event not found")
        current = NotificationStatus(row["status"])
        if current in (NotificationStatus.DELIVERED, NotificationStatus.FAILED):
            connection.commit()
            return _result(row)
        if current not in (
            NotificationStatus.ACCEPTED,
            NotificationStatus.PENDING_RETRY,
        ):
            raise PermanentDeliveryError(
                f"notification status {current.value} is not dispatchable"
            )

        attempts = row["attempts"] + 1
        try:
            payload = _authorised_payload(connection, row)
            adapter.deliver(
                event_id=event_id,
                recipient_id=recipient_id,
                content=payload["content"],
            )
            status = NotificationStatus.DELIVERED
            detail = None
        except RetryableDeliveryError as error:
            status = NotificationStatus.PENDING_RETRY
            detail = _safe_error(error)
        except PermanentDeliveryError as error:
            status = NotificationStatus.FAILED
            detail = _safe_error(error)
        except Exception:
            status = NotificationStatus.PENDING_RETRY
            detail = "delivery adapter error"

        now = _iso(utcnow())
        connection.execute(
            "UPDATE notification_event SET status=?,attempts=?,last_error=?,"
            "updated_at=? WHERE event_id=? AND recipient_id=?",
            (status.value, attempts, detail, now, event_id, recipient_id),
        )
        connection.execute(
            "INSERT INTO audit "
            "(audit_id,actor,action,subject_type,subject_id,event_id,details,at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                new_id(),
                "system:notification-dispatcher",
                "delivery." + status.value,
                "notification",
                f"{event_id}:{recipient_id}",
                event_id,
                json.dumps(
                    {"recipient_id": recipient_id, "attempts": attempts},
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                now,
            ),
        )
        connection.commit()
        updated = connection.execute(
            "SELECT * FROM notification_event WHERE event_id=? AND recipient_id=?",
            (event_id, recipient_id),
        ).fetchone()
        return _result(updated)
    except Exception:
        connection.rollback()
        raise


def dispatch_pending(
    connection: sqlite3.Connection,
    adapter: DeliveryAdapter,
    *,
    limit: int = 100,
) -> tuple[DeliveryResult, ...]:
    """Dispatch a stable snapshot of accepted and retryable events."""

    if limit < 1:
        raise ValueError("limit must be positive")
    keys: Iterable[sqlite3.Row] = connection.execute(
        "SELECT event_id,recipient_id FROM notification_event "
        "WHERE status IN ('accepted','pending_retry') "
        "ORDER BY created_at,event_id,recipient_id LIMIT ?",
        (limit,),
    ).fetchall()
    return tuple(
        dispatch_one(connection, row["event_id"], row["recipient_id"], adapter)
        for row in keys
    )


def _authorised_payload(connection: sqlite3.Connection, row: sqlite3.Row) -> dict:
    try:
        payload = json.loads(row["payload"])
        property_id = payload["property_id"]
        kind = NotificationKind(row["kind"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
        raise PermanentDeliveryError("invalid notification payload") from error
    if not isinstance(payload, dict) or not isinstance(property_id, str):
        raise PermanentDeliveryError("invalid notification payload")

    if kind is NotificationKind.MANAGER_ALERT:
        return _authorised_manager_alert_payload(connection, row, payload)
    return _authorised_tenant_payload(connection, row, payload, kind)


def _authorised_manager_alert_payload(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    payload: dict,
) -> dict:
    """Validate an emergency manager alert without tenant-draft fields."""

    property_id = payload["property_id"]
    message_id = payload.get("message_id")
    if (
        not isinstance(message_id, str)
        or not message_id.strip()
        or row["message_id"] != message_id
        or payload.get("emergency") is not True
    ):
        raise PermanentDeliveryError("invalid manager alert payload")

    source = connection.execute(
        "SELECT property_id FROM message WHERE message_id=?",
        (message_id,),
    ).fetchone()
    if source is None or source["property_id"] != property_id:
        raise PermanentDeliveryError("manager alert source could not be verified")

    authorised = connection.execute(
        "SELECT 1 FROM manager_property WHERE manager_id=? AND property_id=?",
        (row["recipient_id"], property_id),
    ).fetchone()
    if authorised is None:
        raise PermanentDeliveryError("recipient is no longer authorised for property")

    return {
        **payload,
        "content": (
            f"Emergency alert for property {property_id} "
            f"(source message {message_id})."
        ),
    }


def _authorised_tenant_payload(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    payload: dict,
    kind: NotificationKind,
) -> dict:
    """Validate approved or deterministic tenant-facing notifications."""

    try:
        content = payload["content"]
        property_id = payload["property_id"]
        draft_id = payload["draft_id"]
        draft_version = int(payload["draft_version"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
        raise PermanentDeliveryError("invalid approved delivery payload") from error
    if not isinstance(content, str) or not content.strip():
        raise PermanentDeliveryError("approved delivery content is empty")

    approval = connection.execute(
        "SELECT r.final_content,d.status FROM review r "
        "JOIN draft d ON d.draft_id=r.draft_id "
        "WHERE r.draft_id=? AND r.draft_version=? AND r.decision='approved'",
        (draft_id, draft_version),
    ).fetchone()
    approved = (
        approval is not None
        and approval["status"] in ("approved", "sent")
        and approval["final_content"] == content
    )
    if not approved and row["message_id"] and kind in (
        NotificationKind.OTHER,
        NotificationKind.LEASE_RENT_REPLY,
    ):
        return _authorised_communication_auto_payload(connection, row, payload, kind)
    auto_eligible_maintenance = False
    if not approved and kind is NotificationKind.MAINTENANCE_STATUS:
        draft = connection.execute(
            "SELECT source_kind,status,version,content FROM draft WHERE draft_id=?",
            (draft_id,),
        ).fetchone()
        receipt = connection.execute(
            "SELECT tenant_id,property_id,payload FROM "
            "communication_maintenance_receipt WHERE draft_id=?",
            (draft_id,),
        ).fetchone()
        if draft is not None and receipt is not None:
            try:
                accepted_payload = json.loads(receipt["payload"])
            except (json.JSONDecodeError, TypeError):
                accepted_payload = {}
            auto_eligible_maintenance = (
                draft["source_kind"] == "maintenance_status"
                and draft["status"] in ("auto_eligible", "sent")
                and draft["version"] == draft_version
                and draft["content"] == content
                and receipt["tenant_id"] == row["recipient_id"]
                and receipt["property_id"] == property_id
                and accepted_payload.get("review_required") is False
            )
    if not approved and not auto_eligible_maintenance:
        raise PermanentDeliveryError("approved or eligible draft version could not be verified")

    authorised = connection.execute(
        "SELECT 1 FROM tenant_property WHERE tenant_id=? AND property_id=?",
        (row["recipient_id"], property_id),
    ).fetchone()
    if authorised is None:
        raise PermanentDeliveryError("recipient is no longer authorised for property")
    return payload


def _authorised_communication_auto_payload(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    payload: dict,
    kind: NotificationKind,
) -> dict:
    """Recheck a message-origin routine draft before automatic delivery."""
    draft = connection.execute(
        "SELECT d.*,m.tenant_id,m.property_id,m.message_type,m.content AS message_content "
        "FROM draft d JOIN message m ON m.message_id=d.message_id "
        "WHERE d.draft_id=? AND d.message_id=?",
        (payload.get("draft_id"), row["message_id"]),
    ).fetchone()
    if draft is None:
        raise PermanentDeliveryError("communication draft source could not be verified")
    if (
        draft["status"] != "auto_eligible"
        or draft["version"] != int(payload.get("draft_version", -1))
        or draft["content"] != payload.get("content")
        or draft["property_id"] != payload.get("property_id")
        or draft["tenant_id"] != row["recipient_id"]
        or row["message_id"] != draft["message_id"]
    ):
        raise PermanentDeliveryError("communication draft is not an eligible version")
    if kind is NotificationKind.LEASE_RENT_REPLY and draft["message_type"] != "lease_rent":
        raise PermanentDeliveryError("communication notification kind conflicts with source")
    if kind is NotificationKind.OTHER and draft["message_type"] == "lease_rent":
        raise PermanentDeliveryError("communication notification kind conflicts with source")
    emergency = connection.execute(
        "SELECT 1 FROM audit WHERE subject_type='message' AND subject_id=? "
        "AND action='communication.emergency_alert_attempted' LIMIT 1",
        (row["message_id"],),
    ).fetchone()
    if emergency is not None:
        raise PermanentDeliveryError("emergency communication requires review")

    # Reuse the policy's decision vocabulary rather than trusting a payload
    # flag. Import lazily to avoid a communication<->notification import cycle.
    from communication.policy import is_decision_request

    if is_decision_request(draft["message_content"]):
        raise PermanentDeliveryError("decision communication requires review")
    if draft["message_type"] == "unknown":
        raise PermanentDeliveryError("unknown communication requires review")

    try:
        references = json.loads(draft["source_facts"] or "[]")
    except (json.JSONDecodeError, TypeError):
        raise PermanentDeliveryError("communication source facts are invalid") from None
    if not isinstance(references, list):
        raise PermanentDeliveryError("communication source facts are invalid")
    if draft["message_type"] == "maintenance":
        if not references or not _maintenance_reference_exists(
            connection, draft["request_id"], draft["tenant_id"], draft["property_id"]
        ):
            raise PermanentDeliveryError("maintenance source facts are unavailable")
    elif draft["message_type"] == "lease_rent":
        if not references or not all(
            isinstance(reference, dict) and
            _lease_rent_reference_exists(
                connection,
                reference.get("source_type"),
                reference.get("source_id"),
                draft["tenant_id"],
                draft["property_id"],
            )
            for reference in references
        ):
            raise PermanentDeliveryError("lease/rent source facts are unavailable")
    return payload


def _maintenance_reference_exists(
    connection: sqlite3.Connection, request_id: str | None, tenant_id: str, property_id: str
) -> bool:
    if not request_id:
        return False
    return bool(
        connection.execute(
            "SELECT 1 FROM mc_request WHERE id=? AND tenant_id=? AND property_id=?",
            (request_id, tenant_id, property_id),
        ).fetchone()
        or connection.execute(
            "SELECT 1 FROM maintenance_request WHERE request_id=? AND tenant_id=? "
            "AND property_id=?",
            (request_id, tenant_id, property_id),
        ).fetchone()
    )


def _lease_rent_reference_exists(
    connection: sqlite3.Connection,
    source_type: str | None,
    source_id: str | None,
    tenant_id: str,
    property_id: str,
) -> bool:
    if source_type == "rent_record":
        return connection.execute(
            "SELECT 1 FROM rent_record r JOIN lease l ON l.lease_id=r.lease_id "
            "WHERE r.rent_record_id=? AND l.tenant_id=? AND l.property_id=? "
            "AND r.property_id=?",
            (source_id, tenant_id, property_id, property_id),
        ).fetchone() is not None
    if source_type == "lease":
        return connection.execute(
            "SELECT 1 FROM lease WHERE lease_id=? AND tenant_id=? AND property_id=?",
            (source_id, tenant_id, property_id),
        ).fetchone() is not None
    return False


def _result(row: sqlite3.Row) -> DeliveryResult:
    from datetime import datetime

    return DeliveryResult(
        event_id=row["event_id"],
        status=NotificationStatus(row["status"]),
        detail=row["last_error"],
        attempts=row["attempts"],
        checked_at=datetime.fromisoformat(row["updated_at"].replace("Z", "+00:00")),
    )


def _safe_error(error: Exception) -> str:
    message = str(error).strip()
    return message[:240] if message else error.__class__.__name__


def _iso(value) -> str:
    return value.isoformat().replace("+00:00", "Z")
