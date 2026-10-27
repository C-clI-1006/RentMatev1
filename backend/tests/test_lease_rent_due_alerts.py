"""End-to-end UC4/UC5 scheduled draft preparation tests."""

import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from lease_rent import run_due_alerts
from review.service import ReviewService
from shared import AuthContext
from shared.sqlite import init_connection


NOW = datetime(2026, 10, 20, 8, 0, tzinfo=timezone.utc)


@pytest.fixture()
def database(tmp_path):
    path = tmp_path / "rentmate.db"
    connection = init_connection(path)
    connection.executescript(
        """
        INSERT INTO tenant (tenant_id,name,created_at)
        VALUES ('t1','Tenant','2026-01-01T00:00:00Z');
        INSERT INTO property (property_id,name,created_at)
        VALUES ('p1','Home','2026-01-01T00:00:00Z');
        INSERT INTO tenant_property (tenant_id,property_id,created_at)
        VALUES ('t1','p1','2026-01-01T00:00:00Z');
        INSERT INTO manager (manager_id,name,created_at)
        VALUES ('m1','Manager','2026-01-01T00:00:00Z');
        INSERT INTO manager_property (manager_id,property_id,created_at)
        VALUES ('m1','p1','2026-01-01T00:00:00Z');

        INSERT INTO lease
          (lease_id,property_id,tenant_id,start_date,end_date,created_at)
        VALUES
          ('lease-due','p1','t1','2026-01-01','2026-11-19','2026-01-01T00:00:00Z'),
          ('lease-later','p1','t1','2026-01-01','2026-12-31','2026-01-01T00:00:00Z'),
          ('lease-expired','p1','t1','2026-01-01','2026-10-19','2026-01-01T00:00:00Z');

        INSERT INTO rent_record
          (rent_record_id,lease_id,property_id,period,due_date,
           amount_due,amount_paid,status,created_at)
        VALUES
          ('rent-overdue','lease-due','p1','2026-10','2026-10-01',
           1000,250,'overdue','2026-10-01T00:00:00Z'),
          ('rent-paid','lease-due','p1','2026-10','2026-10-01',
           1000,1000,'paid','2026-10-01T00:00:00Z'),
          ('rent-not-due','lease-due','p1','2026-10','2026-10-21',
           1000,0,'unpaid-but-not-due','2026-10-01T00:00:00Z');
        """
    )
    connection.commit()
    connection.close()
    return path


def test_due_alert_run_creates_only_eligible_review_drafts(database):
    result = run_due_alerts(now=NOW, db_path=database)

    assert result == {
        "as_of": "2026-10-20",
        "rent_candidates": 1,
        "lease_candidates": 1,
        "drafts_created": 2,
        "drafts_reused": 0,
        "deadline_alerts_created": 1,
    }

    connection = init_connection(database)
    drafts = connection.execute(
        "SELECT source_kind,source_id,status,review_reason,source_facts "
        "FROM draft ORDER BY source_kind"
    ).fetchall()
    assert [(row["source_kind"], row["status"]) for row in drafts] == [
        ("lease_deadline", "pending_review"),
        ("rent_reminder", "pending_review"),
    ]
    assert drafts[0]["source_id"] == "lease-deadline:lease-due:2026-11-19"
    assert drafts[1]["source_id"] == "rent-overdue:rent-overdue:2026-10-01"
    assert "UC5" in drafts[0]["review_reason"]
    assert "UC4" in drafts[1]["review_reason"]
    assert json.loads(drafts[0]["source_facts"])[0]["source_id"] == "lease-due"
    assert json.loads(drafts[1]["source_facts"])[0]["source_id"] == "rent-overdue"
    assert connection.execute("SELECT COUNT(*) FROM audit").fetchone()[0] == 2
    assert connection.execute(
        "SELECT COUNT(*) FROM lease_deadline_alert"
    ).fetchone()[0] == 1

    queue = ReviewService(
        connection,
        AuthContext(tenant_id="t1", property_id="p1", manager_id="m1"),
    ).list_pending()
    assert len(queue) == 2
    assert {item.property_id for item in queue} == {"p1"}
    connection.close()


def test_due_alert_run_is_idempotent_and_repairs_missing_draft(database):
    first = run_due_alerts(now=NOW, db_path=database)
    second = run_due_alerts(now=NOW, db_path=database)

    assert first["drafts_created"] == 2
    assert second["drafts_created"] == 0
    assert second["drafts_reused"] == 2
    assert second["deadline_alerts_created"] == 0

    connection = init_connection(database)
    connection.execute(
        "DELETE FROM draft WHERE source_kind='lease_deadline'"
    )
    connection.commit()
    connection.close()

    repaired = run_due_alerts(now=NOW, db_path=database)
    assert repaired["drafts_created"] == 1
    assert repaired["drafts_reused"] == 1
    assert repaired["deadline_alerts_created"] == 0

    connection = init_connection(database)
    assert connection.execute("SELECT COUNT(*) FROM draft").fetchone()[0] == 2
    assert connection.execute(
        "SELECT COUNT(*) FROM lease_deadline_alert"
    ).fetchone()[0] == 1
    connection.close()


def test_due_alert_run_requires_timezone_aware_now(database):
    with pytest.raises(ValueError, match="timezone-aware"):
        run_due_alerts(now=datetime(2026, 10, 20, 8, 0), db_path=database)


def test_ops_endpoint_requires_scheduler_token_and_runs(database, tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "wrong-environment.db"))
    monkeypatch.setenv("RENTMATE_SCHEDULER_TOKEN", "scheduler-secret")

    from main import create_app

    with TestClient(
        create_app(db_path=str(database), storage_root=str(tmp_path / "files"))
    ) as client:
        assert client.post(
            "/api/ops/rent-alerts/run?now=2026-10-20T08:00:00Z"
        ).status_code == 401
        response = client.post(
            "/api/ops/rent-alerts/run?now=2026-10-20T08:00:00Z",
            headers={"Authorization": "Bearer scheduler-secret"},
        )
        assert response.status_code == 200
        assert response.json()["drafts_created"] == 2

        invalid = client.post(
            "/api/ops/rent-alerts/run?now=2026-10-20T08:00:00",
            headers={"Authorization": "Bearer scheduler-secret"},
        )
        assert invalid.status_code == 422
