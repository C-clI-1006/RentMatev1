"""HTTP integration checks for the tenant communication entry point."""

import json
import sqlite3

from fastapi.testclient import TestClient

from main import create_app
from shared.sqlite import init_connection


def test_http_communication_uses_server_scope_and_real_module_adapters(
    tmp_path, monkeypatch
):
    database = tmp_path / "communication.db"
    connection = init_connection(database)
    timestamp = "2026-10-26T00:00:00Z"
    connection.execute(
        "INSERT INTO tenant (tenant_id,name,created_at) VALUES ('tenant-1','T',?)",
        (timestamp,),
    )
    connection.execute(
        "INSERT INTO property (property_id,name,created_at) VALUES ('property-1','P',?)",
        (timestamp,),
    )
    connection.execute(
        "INSERT INTO tenant_property (tenant_id,property_id,role,created_at) "
        "VALUES ('tenant-1','property-1','resident',?)",
        (timestamp,),
    )
    connection.commit()
    connection.close()

    monkeypatch.setenv(
        "RENTMATE_AUTH_TOKENS",
        json.dumps(
            {
                "valid-token": {
                    "tenant_id": "tenant-1",
                    "property_id": "property-1",
                },
                "unbound-token": {
                    "tenant_id": "tenant-2",
                    "property_id": "property-2",
                },
            }
        ),
    )
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)

    app = create_app(
        db_path=str(database),
        storage_root=str(tmp_path / "evidence"),
    )
    try:
        with TestClient(app) as client:
            response = client.post(
                "/api/communication/messages",
                headers={"Authorization": "Bearer valid-token"},
                json={"content": "The kitchen sink is broken"},
            )
            forged_scope = client.post(
                "/api/communication/messages",
                headers={"Authorization": "Bearer valid-token"},
                json={
                    "content": "Hello",
                    "tenant_id": "attacker",
                    "property_id": "other-property",
                },
            )
            unbound = client.post(
                "/api/communication/messages",
                headers={"Authorization": "Bearer unbound-token"},
                json={"content": "Hello"},
            )

        assert response.status_code == 200
        assert set(response.json()) == {
            "message_id",
            "reply_status",
            "delivery_status",
        }
        assert response.json()["reply_status"] == "auto_eligible"
        assert response.json()["delivery_status"] == "not_dispatched"
        assert forged_scope.status_code == 422
        assert unbound.status_code == 403

        connection = sqlite3.connect(database)
        try:
            message = connection.execute(
                "SELECT tenant_id,property_id FROM message"
            ).fetchone()
            request = connection.execute("SELECT id FROM mc_request").fetchone()
            draft = connection.execute("SELECT request_id FROM draft").fetchone()
            legacy_count = connection.execute(
                "SELECT COUNT(*) FROM maintenance_request"
            ).fetchone()[0]
        finally:
            connection.close()
        assert message == ("tenant-1", "property-1")
        assert request is not None
        assert draft == request
        assert legacy_count == 0
    finally:
        app.state.evidence_service._repo.close()


def test_http_routine_communication_is_queued_then_dispatched_once(
    tmp_path, monkeypatch
):
    database = tmp_path / "communication-dispatch.db"
    connection = init_connection(database)
    timestamp = "2026-10-29T00:00:00Z"
    connection.execute(
        "INSERT INTO tenant (tenant_id,name,created_at) VALUES ('tenant-1','T',?)",
        (timestamp,),
    )
    connection.execute(
        "INSERT INTO property (property_id,name,created_at) VALUES ('property-1','P',?)",
        (timestamp,),
    )
    connection.execute(
        "INSERT INTO tenant_property (tenant_id,property_id,role,created_at) "
        "VALUES ('tenant-1','property-1','resident',?)",
        (timestamp,),
    )
    connection.commit()
    connection.close()
    monkeypatch.setenv(
        "RENTMATE_AUTH_TOKENS",
        json.dumps({"tenant-token": {"tenant_id": "tenant-1", "property_id": "property-1"}}),
    )
    monkeypatch.setenv("RENTMATE_SCHEDULER_TOKEN", "scheduler-token")
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    app = create_app(db_path=str(database), storage_root=str(tmp_path / "evidence"))
    try:
        with TestClient(app) as client:
            submitted = client.post(
                "/api/communication/messages",
                headers={"Authorization": "Bearer tenant-token"},
                json={"content": "hello"},
            )
            assert submitted.status_code == 200
            assert submitted.json()["reply_status"] == "auto_eligible"
            assert submitted.json()["delivery_status"] == "not_dispatched"
            history = client.get('/api/communication/messages', headers={"Authorization": "Bearer tenant-token"})
            assert history.status_code == 200
            assert history.json()[0]['message_id'] == submitted.json()['message_id']
            assert history.json()[0]['reply_content']
            assert client.get('/api/communication/messages').status_code in (401, 403)
            check = init_connection(database)
            check.execute("UPDATE draft SET status='pending_review' WHERE message_id=?", (submitted.json()['message_id'],))
            check.commit()
            assert client.get('/api/communication/messages', headers={"Authorization": "Bearer tenant-token"}).json()[0]['reply_content'] is None
            check.execute("UPDATE draft SET status='auto_eligible' WHERE message_id=?", (submitted.json()['message_id'],))
            check.commit()
            check.close()
            first = client.post(
                "/api/ops/notifications/dispatch",
                headers={"Authorization": "Bearer scheduler-token"},
            )
            second = client.post(
                "/api/ops/notifications/dispatch",
                headers={"Authorization": "Bearer scheduler-token"},
            )
            inbox = client.get(
                "/api/notifications/inbox",
                headers={"Authorization": "Bearer tenant-token"},
            )
        assert first.status_code == 200
        assert first.json()["delivered"] == 1
        assert second.status_code == 200
        assert second.json()["processed"] == 0
        assert len(inbox.json()) == 1
    finally:
        app.state.evidence_service._repo.close()
