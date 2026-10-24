import json
import sqlite3

from fastapi.testclient import TestClient

from maintenance.seed_demo import seed
from review.api import get_connection


def _connection(path):
    connection = sqlite3.connect(path, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        yield connection
    finally:
        connection.close()


def test_seed_demo_is_repeatable_and_creates_review_scenarios(tmp_path):
    database = tmp_path / "demo.db"

    seed(database)

    # Simulate a completed previous rehearsal. A new demo seed resets only
    # the fixed synthetic fixture and makes it reviewable again.
    connection = sqlite3.connect(database)
    connection.execute(
        "INSERT INTO review "
        "(review_id,draft_id,draft_version,decision,manager_id,final_content,decided_at) "
        "VALUES ('old-review','demo-review-draft',1,'approved','demo-manager','old',"
        "'2026-01-02T00:00:00Z')"
    )
    connection.execute("UPDATE draft SET status='approved' WHERE draft_id='demo-review-draft'")
    connection.commit()
    connection.close()

    seed(database)

    connection = sqlite3.connect(database)
    try:
        assert connection.execute(
            "SELECT manager_id,property_id FROM manager_property "
            "WHERE manager_id LIKE 'demo-manager%' ORDER BY manager_id"
        ).fetchall() == [
            ("demo-manager", "demo-property"),
            ("demo-manager-2", "demo-property-2"),
        ]
        assert connection.execute(
            "SELECT draft_id,status FROM draft WHERE draft_id LIKE 'demo-review-draft%' "
            "ORDER BY draft_id"
        ).fetchall() == [
            ("demo-review-draft", "pending_review"),
            ("demo-review-draft-2", "pending_review"),
        ]
        assert connection.execute(
            "SELECT COUNT(*) FROM message WHERE message_id LIKE 'demo-review-message%'"
        ).fetchone()[0] == 2
        assert connection.execute(
            "SELECT COUNT(*) FROM review WHERE draft_id='demo-review-draft'"
        ).fetchone()[0] == 0
    finally:
        connection.close()


def test_seeded_review_http_scope_and_forbidden_binding(tmp_path, monkeypatch):
    from main import app

    database = tmp_path / "demo.db"
    seed(database)

    def api_connection():
        yield from _connection(database)

    app.dependency_overrides[get_connection] = api_connection
    monkeypatch.setenv(
        "RENTMATE_AUTH_TOKENS",
        json.dumps(
            {
                "manager-1": {
                    "tenant_id": "demo-tenant",
                    "property_id": "demo-property",
                    "manager_id": "demo-manager",
                },
                "manager-2": {
                    "tenant_id": "demo-tenant-2",
                    "property_id": "demo-property-2",
                    "manager_id": "demo-manager-2",
                },
                "forbidden": {
                    "tenant_id": "demo-tenant-2",
                    "property_id": "demo-property-2",
                    "manager_id": "demo-manager",
                },
            }
        ),
    )
    try:
        with TestClient(app) as client:
            first = client.get(
                "/api/reviews", headers={"Authorization": "Bearer manager-1"}
            )
            second = client.get(
                "/api/reviews", headers={"Authorization": "Bearer manager-2"}
            )
            denied = client.get(
                "/api/reviews", headers={"Authorization": "Bearer forbidden"}
            )

        assert first.status_code == 200
        assert [item["draft"]["draft_id"] for item in first.json()] == [
            "demo-review-draft"
        ]
        assert second.status_code == 200
        assert [item["draft"]["draft_id"] for item in second.json()] == [
            "demo-review-draft-2"
        ]
        assert denied.status_code == 403
    finally:
        app.dependency_overrides.clear()
