import json
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from contracts import EventId, NotificationEvent, NotificationKind, RecipientId
from contracts.drafting import ReviewDecision
from notification import (
    NotificationConflictError,
    PermanentDeliveryError,
    RetryableDeliveryError,
    SQLiteNotificationInboxAdapter,
    SQLiteNotificationService,
    dispatch_one,
)
from review.service import ReviewService
from shared import AuthContext, utcnow
from shared.sqlite import init_connection


TS = "2026-10-25T00:00:00Z"


@pytest.fixture()
def db(tmp_path):
    connection = init_connection(tmp_path / "notification.db")
    connection.executescript(
        f"""
        INSERT INTO tenant (tenant_id,name,created_at) VALUES ('t1','Tenant','{TS}');
        INSERT INTO property (property_id,name,created_at) VALUES ('p1','Property','{TS}');
        INSERT INTO tenant_property (tenant_id,property_id,created_at)
          VALUES ('t1','p1','{TS}');
        INSERT INTO manager (manager_id,name,created_at) VALUES ('m1','Manager','{TS}');
        INSERT INTO manager_property (manager_id,property_id,created_at)
          VALUES ('m1','p1','{TS}');
        INSERT INTO lease (lease_id,property_id,tenant_id,start_date,end_date,created_at)
          VALUES ('l1','p1','t1','2026-01-01','2026-12-31','{TS}');
        INSERT INTO rent_record
          (rent_record_id,lease_id,property_id,period,due_date,status,created_at)
          VALUES ('rr1','l1','p1','2026-10','2026-10-01','overdue','{TS}');
        INSERT INTO draft
          (draft_id,source_kind,source_id,content,review_reason,status,created_at)
          VALUES ('d1','rent_reminder','rent-overdue:rr1:2026-10-01',
                  'Exact approved reminder','scheduled reminder','pending_review','{TS}');
        """
    )
    connection.commit()
    yield connection
    connection.close()


class RecordingAdapter:
    def __init__(self, outcomes=()):
        self.outcomes = list(outcomes)
        self.calls = []

    def deliver(self, **delivery):
        self.calls.append(delivery)
        if self.outcomes:
            outcome = self.outcomes.pop(0)
            if outcome is not None:
                raise outcome


def approve(db):
    return ReviewService(db, AuthContext("t1", "p1", "m1")).decide(
        "d1", expected_version=1, decision=ReviewDecision.APPROVED
    )


def queue_manager_alert(db):
    db.execute(
        "INSERT INTO message "
        "(message_id,tenant_id,property_id,content,message_type,received_at) "
        "VALUES ('message-emergency','t1','p1','Gas leak','maintenance',?)",
        (TS,),
    )
    db.commit()
    return SQLiteNotificationService(db).publish(
        NotificationEvent(
            event_id=EventId("alert:message-emergency"),
            recipient_id=RecipientId("m1"),
            kind=NotificationKind.MANAGER_ALERT,
            message_id="message-emergency",
            payload={
                "property_id": "p1",
                "message_id": "message-emergency",
                "emergency": True,
            },
            created_at=utcnow(),
        )
    )


def test_approval_queues_exact_version_then_dispatches_once(db):
    approve(db)
    adapter = RecordingAdapter()
    delivered = dispatch_one(db, "draft:d1:v1", "t1", adapter)
    assert delivered.status.value == "delivered"
    assert delivered.attempts == 1
    assert adapter.calls == [
        {
            "event_id": "draft:d1:v1",
            "recipient_id": "t1",
            "content": "Exact approved reminder",
        }
    ]

    replay = dispatch_one(db, "draft:d1:v1", "t1", adapter)
    assert replay.status.value == "delivered"
    assert replay.attempts == 1
    assert len(adapter.calls) == 1
    assert db.execute(
        "SELECT 1 FROM audit WHERE action='delivery.delivered'"
    ).fetchone()


def test_retryable_failure_is_retained_and_retried_without_duplicate_event(db):
    approve(db)
    adapter = RecordingAdapter([RetryableDeliveryError("temporary outage"), None])
    first = dispatch_one(db, "draft:d1:v1", "t1", adapter)
    assert first.status.value == "pending_retry"
    assert first.detail == "temporary outage"
    second = dispatch_one(db, "draft:d1:v1", "t1", adapter)
    assert second.status.value == "delivered"
    assert second.attempts == 2
    assert db.execute("SELECT count(*) FROM notification_event").fetchone()[0] == 1


def test_permanent_failure_is_recorded_and_not_retried(db):
    approve(db)
    adapter = RecordingAdapter([PermanentDeliveryError("invalid destination")])
    first = dispatch_one(db, "draft:d1:v1", "t1", adapter)
    second = dispatch_one(db, "draft:d1:v1", "t1", adapter)
    assert first.status.value == second.status.value == "failed"
    assert first.attempts == second.attempts == 1
    assert len(adapter.calls) == 1


def test_dispatch_rechecks_property_binding(db):
    approve(db)
    db.execute("DELETE FROM tenant_property WHERE tenant_id='t1' AND property_id='p1'")
    db.commit()
    result = dispatch_one(db, "draft:d1:v1", "t1", RecordingAdapter())
    assert result.status.value == "failed"
    assert result.detail == "recipient is no longer authorised for property"
    row = db.execute(
        "SELECT status,attempts FROM notification_event WHERE event_id='draft:d1:v1'"
    ).fetchone()
    assert tuple(row) == ("failed", 1)


def test_manager_alert_dispatches_without_tenant_draft_fields(db):
    queue_manager_alert(db)
    delivered = dispatch_one(
        db,
        "alert:message-emergency",
        "m1",
        SQLiteNotificationInboxAdapter(db),
    )
    assert delivered.status.value == "delivered"
    assert delivered.attempts == 1
    row = db.execute(
        "SELECT property_id,content FROM manager_inbox "
        "WHERE event_id='alert:message-emergency' AND recipient_id='m1'"
    ).fetchone()
    assert tuple(row) == (
        "p1",
        "Emergency alert for property p1 (source message message-emergency).",
    )
    assert db.execute("SELECT count(*) FROM tenant_inbox").fetchone()[0] == 0


def test_manager_alert_dispatch_rejects_removed_binding(db):
    queue_manager_alert(db)
    db.execute(
        "DELETE FROM manager_property WHERE manager_id='m1' AND property_id='p1'"
    )
    db.commit()
    result = dispatch_one(
        db,
        "alert:message-emergency",
        "m1",
        SQLiteNotificationInboxAdapter(db),
    )
    assert result.status.value == "failed"
    assert result.detail == "recipient is no longer authorised for property"
    assert db.execute("SELECT count(*) FROM manager_inbox").fetchone()[0] == 0


def test_manager_alert_dispatch_is_idempotent(db):
    queue_manager_alert(db)
    adapter = SQLiteNotificationInboxAdapter(db)
    first = dispatch_one(db, "alert:message-emergency", "m1", adapter)
    replay = dispatch_one(db, "alert:message-emergency", "m1", adapter)
    assert first.status.value == replay.status.value == "delivered"
    assert first.attempts == replay.attempts == 1
    assert db.execute("SELECT count(*) FROM manager_inbox").fetchone()[0] == 1


def test_publish_is_idempotent_and_rejects_conflicting_reuse(db):
    service = SQLiteNotificationService(db)
    event = NotificationEvent(
        event_id=EventId("manual-1"),
        recipient_id=RecipientId("t1"),
        kind=NotificationKind.OTHER,
        payload={"x": 1},
        created_at=utcnow(),
    )
    first = service.publish(event)
    # The stable delivery key and content define the replay. A caller that
    # reconstructs the same logical event later may supply a later timestamp.
    second = service.publish(
        event.model_copy(update={"created_at": event.created_at + timedelta(seconds=1)})
    )
    assert first.status.value == second.status.value == "accepted"
    assert db.execute(
        "SELECT count(*) FROM notification_event WHERE event_id='manual-1'"
    ).fetchone()[0] == 1
    changed = event.model_copy(update={"payload": {"x": 2}})
    with pytest.raises(NotificationConflictError):
        service.publish(changed)


def test_ops_dispatch_and_tenant_inbox_http_flow(db, monkeypatch, tmp_path):
    from main import create_app

    approve(db)
    database = db.execute("PRAGMA database_list").fetchone()[2]
    monkeypatch.setenv("RENTMATE_SCHEDULER_TOKEN", "scheduler-secret")
    monkeypatch.setenv(
        "RENTMATE_AUTH_TOKENS",
        json.dumps(
            {"tenant-secret": {"tenant_id": "t1", "property_id": "p1"}}
        ),
    )
    app = create_app(database, str(tmp_path / "evidence"))
    try:
        with TestClient(app) as client:
            unauthorised = client.post("/api/ops/notifications/dispatch")
            assert unauthorised.status_code in (401, 403)
            dispatched = client.post(
                "/api/ops/notifications/dispatch",
                headers={"Authorization": "Bearer scheduler-secret"},
            )
            assert dispatched.status_code == 200
            assert dispatched.json() == {
                "processed": 1,
                "delivered": 1,
                "pending_retry": 0,
                "failed": 0,
            }
            inbox = client.get(
                "/api/notifications/inbox",
                headers={"Authorization": "Bearer tenant-secret"},
            )
            assert inbox.status_code == 200
            assert inbox.json()[0]["event_id"] == "draft:d1:v1"
            assert inbox.json()[0]["content"] == "Exact approved reminder"
    finally:
        app.state.evidence_service._repo.close()
