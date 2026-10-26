"""Durable notification outbox backed by the shared SQLite database.

Publishing only records an accepted delivery request. It never reports a
message as delivered; the dispatcher is the single component allowed to move
an event to ``delivered`` or a failure state.
"""

from __future__ import annotations

import json
import sqlite3

from contracts import (
    DeliveryResult,
    INotificationService,
    NotificationEvent,
    NotificationStatus,
)
from shared import utcnow


class NotificationNotFoundError(LookupError):
    """Raised when a delivery key is not present in the durable outbox."""


class NotificationConflictError(RuntimeError):
    """Raised when a delivery key is reused with different content."""


class SQLiteNotificationService(INotificationService):
    """Idempotent publisher/status reader for ``notification_event``.

    ``commit_changes=False`` is used by manager review so the immutable review
    decision and its notification event are committed atomically.
    """

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        commit_changes: bool = True,
    ) -> None:
        self._connection = connection
        self._commit_changes = commit_changes

    def publish(self, event: NotificationEvent) -> DeliveryResult:
        payload = json.dumps(
            event.payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        created_at = _iso(event.created_at)
        now = _iso(utcnow())
        inserted = self._connection.execute(
            "INSERT OR IGNORE INTO notification_event "
            "(event_id,recipient_id,kind,request_id,message_id,payload,status,"
            "attempts,last_error,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,'accepted',0,NULL,?,?)",
            (
                str(event.event_id),
                str(event.recipient_id),
                event.kind.value,
                str(event.request_id) if event.request_id is not None else None,
                str(event.message_id) if event.message_id is not None else None,
                payload,
                created_at,
                now,
            ),
        )
        row = self._get_row(str(event.event_id), str(event.recipient_id))
        expected = (
            event.kind.value,
            str(event.request_id) if event.request_id is not None else None,
            str(event.message_id) if event.message_id is not None else None,
            payload,
        )
        actual = (
            row["kind"],
            row["request_id"],
            row["message_id"],
            row["payload"],
        )
        if inserted.rowcount == 0 and actual != expected:
            raise NotificationConflictError(
                "notification delivery key was reused with different content"
            )
        if self._commit_changes:
            self._connection.commit()
        return _result(row)

    def status(self, event_id, recipient_id) -> DeliveryResult:
        return _result(self._get_row(str(event_id), str(recipient_id)))

    def _get_row(self, event_id: str, recipient_id: str) -> sqlite3.Row:
        row = self._connection.execute(
            "SELECT * FROM notification_event WHERE event_id=? AND recipient_id=?",
            (event_id, recipient_id),
        ).fetchone()
        if row is None:
            raise NotificationNotFoundError("notification event not found")
        return row


def _result(row: sqlite3.Row) -> DeliveryResult:
    return DeliveryResult(
        event_id=row["event_id"],
        status=NotificationStatus(row["status"]),
        detail=row["last_error"],
        attempts=row["attempts"],
        checked_at=_datetime(row["updated_at"]),
    )


def _iso(value) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _datetime(value: str):
    from datetime import datetime

    return datetime.fromisoformat(value.replace("Z", "+00:00"))
