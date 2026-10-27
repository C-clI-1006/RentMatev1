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
