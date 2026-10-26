"""Real Maintenance outbox -> Communication draft -> Notification tests."""

import json

import pytest
from fastapi.testclient import TestClient

from communication import MaintenanceCommunicationReceiver
from contracts.drafting import ReviewDecision
from contracts.maintenance import MaintenanceIntake, Priority
from llm import Assessment
from maintenance.outbox import PermanentHandoffError
from maintenance.outbox import dispatch_pending as dispatch_maintenance
from maintenance.service import MaintenanceService
from maintenance.store import Store
from main import create_app
from notification import SQLiteTenantInboxAdapter
from notification import dispatch_pending as dispatch_notifications
from review.service import ReviewService
from shared import AuthContext, utcnow
from shared.sqlite import init_connection


class RoutineAssessor:
    def assess(self, **_kwargs):
        return Assessment(
            Priority.ROUTINE,
            "Private assessor rationale must not reach the tenant.",
            0.9,
            False,
            source="stub",
        )


@pytest.fixture()
def integrated(tmp_path):
    path = tmp_path / "maintenance-communication.db"
    connection = init_connection(path)
    connection.executescript(
        """
        INSERT INTO tenant (tenant_id,name,created_at)
          VALUES ('t1','Tenant 1','2026-10-26T00:00:00Z');
        INSERT INTO property (property_id,name,created_at)
          VALUES ('p1','Property 1','2026-10-26T00:00:00Z');
        INSERT INTO tenant_property (tenant_id,property_id,created_at)
          VALUES ('t1','p1','2026-10-26T00:00:00Z');
        INSERT INTO manager (manager_id,name,created_at)
          VALUES ('m1','Manager 1','2026-10-26T00:00:00Z');
        INSERT INTO manager_property (manager_id,property_id,created_at)
          VALUES ('m1','p1','2026-10-26T00:00:00Z');
        """
    )
    connection.commit()
    store = Store(path)
    try:
        yield path, connection, store
    finally:
        connection.close()


def submit(service, *, emergency=False):
    return service.submit_issue(
        MaintenanceIntake(
            message_id="message-emergency" if emergency else "message-routine",
            event_id="intake-emergency" if emergency else "intake-routine",
            tenant_id="t1",
            property_id="p1",
            title="Gas leak" if emergency else "Loose handle",
            description=(
                "Strong gas smell and hissing from the pipe."
                if emergency
                else "The kitchen cupboard handle is loose."
            ),
            submitted_at=utcnow(),
            emergency_flag=emergency,
        )
    )


def test_routine_events_are_durable_replay_safe_and_delivered(integrated):
    _path, connection, store = integrated
    service = MaintenanceService(
        store, RoutineAssessor(), AuthContext("t1", "p1")
    )
    result = submit(service)
    assert result.persisted_status.value == "assessed"

    receiver = MaintenanceCommunicationReceiver(connection)
    handed_off = dispatch_maintenance(
        store, {"communication": receiver.receive}
    )
    assert [item["status"] for item in handed_off] == [
        "handed_off",
        "handed_off",
    ]
    assert connection.execute(
        "SELECT count(*) FROM communication_maintenance_receipt"
    ).fetchone()[0] == 2
    assert connection.execute(
        "SELECT count(*) FROM draft WHERE source_kind='maintenance_status' "
        "AND status='auto_eligible'"
    ).fetchone()[0] == 2
    assert connection.execute(
        "SELECT count(*) FROM notification_event WHERE kind='maintenance_status' "
        "AND status='accepted'"
    ).fetchone()[0] == 2

    receipt = connection.execute(
        "SELECT payload FROM communication_maintenance_receipt "
        "ORDER BY received_at,event_id LIMIT 1"
    ).fetchone()
    assert receiver.receive(json.loads(receipt["payload"])) is True
    assert connection.execute(
        "SELECT count(*) FROM communication_maintenance_receipt"
    ).fetchone()[0] == 2

    delivered = dispatch_notifications(
        connection, SQLiteTenantInboxAdapter(connection)
    )
    assert [item.status.value for item in delivered] == [
        "delivered",
        "delivered",
    ]
    inbox = connection.execute(
        "SELECT content FROM tenant_inbox ORDER BY delivered_at,event_id"
    ).fetchall()
    assert len(inbox) == 2
    assert all("Private assessor rationale" not in row["content"] for row in inbox)


def test_conflicting_replay_is_permanently_rejected(integrated):
    _path, connection, store = integrated
    service = MaintenanceService(
        store, RoutineAssessor(), AuthContext("t1", "p1")
    )
    submit(service)
    receiver = MaintenanceCommunicationReceiver(connection)
    dispatch_maintenance(store, {"communication": receiver.receive}, limit=1)
    payload = json.loads(
        connection.execute(
            "SELECT payload FROM communication_maintenance_receipt LIMIT 1"
        ).fetchone()["payload"]
    )
    payload["actor"] = "system:tampered"
    with pytest.raises(PermanentHandoffError, match="event_id_conflict"):
        receiver.receive(payload)


def test_emergency_events_wait_for_manager_review(integrated):
    _path, connection, store = integrated
    service = MaintenanceService(
        store, RoutineAssessor(), AuthContext("t1", "p1")
    )
    submit(service, emergency=True)
    receiver = MaintenanceCommunicationReceiver(connection)
    handed_off = dispatch_maintenance(
        store, {"communication": receiver.receive}
    )
    assert [item["status"] for item in handed_off] == [
        "handed_off",
        "handed_off",
    ]
    assert connection.execute(
        "SELECT count(*) FROM draft WHERE source_kind='maintenance_status' "
        "AND status='pending_review'"
    ).fetchone()[0] == 2
    assert connection.execute(
        "SELECT count(*) FROM notification_event"
    ).fetchone()[0] == 0

    reviews = ReviewService(
        connection, AuthContext("t1", "p1", "m1")
    )
    item = reviews.list_pending()[0]
    reviews.decide(
        str(item.draft.draft_id),
        expected_version=item.draft.version,
        decision=ReviewDecision.APPROVED,
    )
    queued = connection.execute(
        "SELECT kind,recipient_id,status FROM notification_event"
    ).fetchone()
    assert tuple(queued) == ("maintenance_status", "t1", "accepted")
    delivered = dispatch_notifications(
        connection, SQLiteTenantInboxAdapter(connection)
    )
    assert len(delivered) == 1
    assert delivered[0].status.value == "delivered"


def test_emergency_cannot_bypass_review_policy(integrated):
    _path, connection, store = integrated
    service = MaintenanceService(
        store, RoutineAssessor(), AuthContext("t1", "p1")
    )
    submit(service, emergency=True)
    with store.transaction() as db:
        payload = json.loads(
            db.execute(
                "SELECT document FROM mc_outbox WHERE target='communication' "
                "ORDER BY rowid LIMIT 1"
            ).fetchone()["document"]
        )
    payload["review_required"] = False
    with pytest.raises(PermanentHandoffError, match="event_payload_conflict"):
        MaintenanceCommunicationReceiver(connection).receive(payload)


def test_ops_routes_execute_complete_maintenance_notification_handoff(
    integrated, tmp_path, monkeypatch
):
    path, connection, store = integrated
    service = MaintenanceService(
        store, RoutineAssessor(), AuthContext("t1", "p1")
    )
    submit(service)
    monkeypatch.setenv("RENTMATE_SCHEDULER_TOKEN", "scheduler-secret")
    monkeypatch.setenv(
        "RENTMATE_AUTH_TOKENS",
        json.dumps(
            {"tenant-token": {"tenant_id": "t1", "property_id": "p1"}}
        ),
    )
    app = create_app(
        db_path=str(path),
        storage_root=str(tmp_path / "evidence"),
    )
    try:
        with TestClient(app) as client:
            unauthorised = client.post(
                "/api/ops/communication/maintenance-events/run"
            )
            handed_off = client.post(
                "/api/ops/communication/maintenance-events/run",
                headers={"Authorization": "Bearer scheduler-secret"},
            )
            delivered = client.post(
                "/api/ops/notifications/dispatch",
                headers={"Authorization": "Bearer scheduler-secret"},
            )
            inbox = client.get(
                "/api/notifications/inbox",
                headers={"Authorization": "Bearer tenant-token"},
            )

        assert unauthorised.status_code == 401
        assert handed_off.status_code == 200
        assert handed_off.json() == {
            "processed": 2,
            "handed_off": 2,
            "retry": 0,
            "rejected": 0,
            "uncertain": 0,
        }
        assert delivered.status_code == 200
        assert delivered.json()["delivered"] == 2
        assert inbox.status_code == 200
        assert len(inbox.json()) == 2
        assert connection.execute(
            "SELECT count(*) FROM mc_outbox "
            "WHERE target='communication' AND status='handed_off'"
        ).fetchone()[0] == 2
    finally:
        app.state.evidence_service._repo.close()
