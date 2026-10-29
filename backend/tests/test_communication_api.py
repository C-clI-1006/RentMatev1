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


def test_http_message_trace_uses_exact_persisted_links_and_conceals_other_property(
    tmp_path, monkeypatch
):
    database = tmp_path / "communication-trace.db"
    connection = init_connection(database)
    timestamp = "2026-10-29T00:00:00Z"
    connection.executescript(
        f"""
        INSERT INTO tenant (tenant_id,name,created_at) VALUES
          ('tenant-1','T1','{timestamp}'),('tenant-2','T2','{timestamp}');
        INSERT INTO property (property_id,name,created_at) VALUES
          ('property-1','P1','{timestamp}'),('property-2','P2','{timestamp}');
        INSERT INTO tenant_property (tenant_id,property_id,role,created_at) VALUES
          ('tenant-1','property-1','resident','{timestamp}'),
          ('tenant-2','property-2','resident','{timestamp}');
        INSERT INTO manager (manager_id,name,created_at)
          VALUES ('manager-1','M1','{timestamp}');
        INSERT INTO manager_property (manager_id,property_id,created_at)
          VALUES ('manager-1','property-1','{timestamp}');
        """
    )
    connection.commit()
    connection.close()
    monkeypatch.setenv(
        "RENTMATE_AUTH_TOKENS",
        json.dumps(
            {
                "tenant-1-token": {
                    "tenant_id": "tenant-1",
                    "property_id": "property-1",
                },
                "tenant-2-token": {
                    "tenant_id": "tenant-2",
                    "property_id": "property-2",
                },
                "manager-token": {
                    "tenant_id": "tenant-1",
                    "property_id": "property-1",
                    "manager_id": "manager-1",
                },
            }
        ),
    )
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    app = create_app(db_path=str(database), storage_root=str(tmp_path / "evidence"))
    try:
        with TestClient(app) as client:
            submitted = client.post(
                "/api/communication/messages",
                headers={"Authorization": "Bearer tenant-1-token"},
                json={"content": "There is a strong smell of gas in my kitchen."},
            )
            assert submitted.status_code == 200
            body = submitted.json()
            assert body["reply_status"] == "pending_review"
            assert body["delivery_status"] == "not_dispatched"

            message_id = body["message_id"]
            trace = client.get(
                f"/api/communication/messages/{message_id}/trace",
                headers={"Authorization": "Bearer tenant-1-token"},
            )
            concealed = client.get(
                f"/api/communication/messages/{message_id}/trace",
                headers={"Authorization": "Bearer tenant-2-token"},
            )

            assert trace.status_code == 200
            detail = trace.json()
            assert detail["classification"] == "maintenance"
            assert detail["emergency"] is True
            assert detail["maintenance_request_id"]
            assert detail["review_draft_id"]
            assert concealed.status_code == 404

            maintenance = client.get(
                f"/api/maintenance/requests/{detail['maintenance_request_id']}",
                headers={"Authorization": "Bearer tenant-1-token"},
            )
            review = client.get(
                f"/api/reviews/{detail['review_draft_id']}",
                headers={"Authorization": "Bearer manager-token"},
            )
            assert maintenance.status_code == 200
            assert review.status_code == 200
            assert review.json()["draft"]["message_id"] == message_id
            assert review.json()["draft"]["request_id"] == detail["maintenance_request_id"]

        connection = init_connection(database)
        try:
            linked_request = connection.execute(
                "SELECT id FROM mc_request WHERE message_id=?", (message_id,)
            ).fetchone()["id"]
            linked_draft = connection.execute(
                "SELECT draft_id FROM draft WHERE message_id=?", (message_id,)
            ).fetchone()["draft_id"]
            delivered = connection.execute(
                "SELECT COUNT(*) FROM tenant_inbox"
            ).fetchone()[0]
        finally:
            connection.close()
        assert linked_request == detail["maintenance_request_id"]
        assert linked_draft == detail["review_draft_id"]
        assert delivered == 0
    finally:
        app.state.evidence_service._repo.close()
