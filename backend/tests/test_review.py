import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from contracts.drafting import ReviewDecision
from permissions import ManagerAccessDeniedError
from review.service import (
    InvalidReviewDecisionError,
    ReviewConflictError,
    ReviewService,
)
from shared import AuthContext
from shared.sqlite import init_connection

TS = "2026-10-24T00:00:00Z"


@pytest.fixture()
def db(tmp_path) -> sqlite3.Connection:
    connection = init_connection(tmp_path / "review.db")
    connection.executescript(
        f"""
        INSERT INTO tenant (tenant_id,name,created_at) VALUES
          ('t1','Tenant 1','{TS}'), ('t2','Tenant 2','{TS}');
        INSERT INTO property (property_id,name,created_at) VALUES
          ('p1','Property 1','{TS}'), ('p2','Property 2','{TS}');
        INSERT INTO tenant_property (tenant_id,property_id,created_at) VALUES
          ('t1','p1','{TS}'), ('t2','p2','{TS}');
        INSERT INTO manager (manager_id,name,created_at) VALUES
          ('m1','Manager 1','{TS}'), ('m2','Manager 2','{TS}');
        INSERT INTO manager_property (manager_id,property_id,created_at) VALUES
          ('m1','p1','{TS}'), ('m2','p2','{TS}');
        INSERT INTO message
          (message_id,tenant_id,property_id,content,message_type,received_at) VALUES
          ('msg1','t1','p1','Please waive my rent','lease_rent','{TS}'),
          ('msg2','t2','p2','Please change my lease','lease_rent','{TS}');
        INSERT INTO draft
          (draft_id,message_id,version,content,review_reason,source_facts,status,created_at) VALUES
          ('d1','msg1',2,'Manager review draft','decision_request','[]','pending_review','{TS}'),
          ('d-auto','msg1',1,'Routine reply',NULL,'[]','auto_eligible','{TS}'),
          ('d2','msg2',1,'Other property draft','decision_request','[]','pending_review','{TS}');
        """
    )
    connection.commit()
    yield connection
    connection.close()


def service(db, manager_id="m1", property_id="p1") -> ReviewService:
    return ReviewService(db, AuthContext("t1", property_id, manager_id))


def test_pending_queue_is_property_scoped_and_contains_context(db):
    items = service(db).list_pending()
    assert [item.draft.draft_id for item in items] == ["d1"]
    assert items[0].draft.version == 2
    assert items[0].original_message == "Please waive my rent"
    assert items[0].reason == "decision_request"


def test_approval_binds_exact_version_and_edited_content(db):
    record = service(db).decide(
        "d1",
        expected_version=2,
        decision=ReviewDecision.APPROVED,
        final_content="The manager-approved exact reply.",
    )
    assert record.draft_version == 2
    assert record.manager_id == "m1"
    assert record.final_content == "The manager-approved exact reply."
    assert db.execute("SELECT status FROM draft WHERE draft_id='d1'").fetchone()[0] == "approved"
    stored = db.execute(
        "SELECT decision,final_content FROM review WHERE draft_id='d1'"
    ).fetchone()
    assert tuple(stored) == ("approved", "The manager-approved exact reply.")
    assert db.execute("SELECT count(*) FROM notification_event").fetchone()[0] == 0
    assert db.execute(
        "SELECT 1 FROM audit WHERE action='review.approved' AND subject_id='d1'"
    ).fetchone()


def test_approval_without_edit_uses_exact_draft_content(db):
    record = service(db).decide(
        "d1", expected_version=2, decision=ReviewDecision.APPROVED
    )
    assert record.final_content == "Manager review draft"


def test_rejection_requires_reason_and_never_creates_content_or_notification(db):
    with pytest.raises(InvalidReviewDecisionError):
        service(db).decide("d1", expected_version=2, decision=ReviewDecision.REJECTED)
    record = service(db).decide(
        "d1",
        expected_version=2,
        decision=ReviewDecision.REJECTED,
        reason="The proposed response is not appropriate.",
    )
    assert record.final_content is None
    assert db.execute("SELECT status FROM draft WHERE draft_id='d1'").fetchone()[0] == "rejected"
    assert db.execute("SELECT count(*) FROM notification_event").fetchone()[0] == 0


def test_stale_or_repeated_decision_is_rejected_without_overwrite(db):
    with pytest.raises(ReviewConflictError, match="stale"):
        service(db).decide("d1", expected_version=1, decision=ReviewDecision.APPROVED)
    first = service(db).decide(
        "d1", expected_version=2, decision=ReviewDecision.APPROVED
    )
    with pytest.raises(ReviewConflictError, match="no longer pending"):
        service(db).decide(
            "d1",
            expected_version=2,
            decision=ReviewDecision.REJECTED,
            reason="Second decision",
        )
    assert db.execute("SELECT review_id FROM review WHERE draft_id='d1'").fetchone()[0] == first.review_id


def test_unbound_manager_is_default_denied_before_queue_access(db):
    with pytest.raises(ManagerAccessDeniedError):
        service(db, manager_id="m2").list_pending()
    with pytest.raises(ManagerAccessDeniedError):
        service(db, manager_id="unknown").get_pending("d1")


def test_system_rent_reminder_resolves_property_scope(db):
    db.executescript(
        f"""
        INSERT INTO lease (lease_id,property_id,tenant_id,start_date,end_date,created_at)
          VALUES ('l1','p1','t1','2026-01-01','2026-12-31','{TS}');
        INSERT INTO rent_record
          (rent_record_id,lease_id,property_id,period,due_date,status,created_at)
          VALUES ('rr1','l1','p1','2026-10','2026-10-01','overdue','{TS}');
        INSERT INTO draft
          (draft_id,source_kind,source_id,content,review_reason,status,created_at)
          VALUES ('rent-draft','rent_reminder','rent-overdue:rr1:2026-10-01',
                  'Rent reminder','scheduled rent reminder','pending_review','{TS}');
        """
    )
    db.commit()
    item = service(db).get_pending("rent-draft")
    assert item.property_id == "p1"
    assert item.original_message is None


def test_review_api_auth_scope_and_decision(db, monkeypatch):
    from main import app
    from review.api import get_connection

    database = db.execute("PRAGMA database_list").fetchone()[2]

    def api_connection():
        connection = sqlite3.connect(database, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
        finally:
            connection.close()

    app.dependency_overrides[get_connection] = api_connection
    monkeypatch.setenv(
        "RENTMATE_AUTH_TOKENS",
        json.dumps(
            {
                "manager": {"tenant_id": "t1", "property_id": "p1", "manager_id": "m1"},
                "unbound": {"tenant_id": "t1", "property_id": "p1", "manager_id": "m2"},
                "tenant": {"tenant_id": "t1", "property_id": "p1"},
            }
        ),
    )
    try:
        with TestClient(app) as client:
            headers = {"Authorization": "Bearer manager"}
            assert client.get("/api/reviews", headers=headers).json()[0]["draft"]["draft_id"] == "d1"
            for token in ("unbound", "tenant"):
                response = client.get(
                    "/api/reviews", headers={"Authorization": "Bearer " + token}
                )
                assert response.status_code == 403
            response = client.post(
                "/api/reviews/d1/decision",
                headers=headers,
                json={
                    "expected_version": 2,
                    "decision": "approved",
                    "final_content": "Approved through API",
                    "manager_id": "spoofed",
                },
            )
            assert response.status_code == 422
            response = client.post(
                "/api/reviews/d1/decision",
                headers=headers,
                json={
                    "expected_version": 2,
                    "decision": "approved",
                    "final_content": "Approved through API",
                },
            )
            assert response.status_code == 200
            assert response.json()["manager_id"] == "m1"
    finally:
        app.dependency_overrides.clear()
