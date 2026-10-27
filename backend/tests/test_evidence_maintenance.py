"""Real SQLite + existing Maintenance outbox; only inference is stubbed."""
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from functools import partial

import pytest
from fastapi.testclient import TestClient

from contracts.evidence import EvidenceUploadCommand
from evidence.maintenance import EvidenceReferenceError
from evidence.router import build_evidence_service
from llm.classify import StubDocumentClassifier
from maintenance.outbox import PermanentHandoffError, RetryableHandoffError, dispatch_pending, reconcile
from maintenance.service import MaintenanceService
from shared import AuthContext
from shared.sqlite import SCHEMA_VERSION, init_connection
from test_maintenance import setup, intake, command
from contracts.maintenance import MaintenanceStatus as S


@pytest.fixture
def integrated(setup, tmp_path):
    store, tenant, manager = setup
    evidence = build_evidence_service(store.path, str(tmp_path / "uploads"), StubDocumentClassifier())
    uploaded = evidence.upload(EvidenceUploadCommand(
        property_id="p1", original_filename="completion.txt", content=b"repair completed",
    ), tenant.auth)
    doc = uploaded.document.document_id
    request = tenant.submit_issue(intake())
    with store.transaction() as db:
        payload = json.loads(db.execute(
            "SELECT document FROM mc_outbox WHERE target='evidence' ORDER BY rowid LIMIT 1",
        ).fetchone()[0])
    payload["evidence_refs"] = [doc]
    try:
        yield store, tenant, manager, evidence, request.request_id, doc, payload
    finally:
        evidence._repo.close()


def counts(store):
    with store.transaction() as db:
        return tuple(db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in (
            "evidence_event_receipt", "evidence_maintenance_timeline", "evidence_event_document",
        ))


def test_receive_atomic_durable_replay_and_timeline(integrated, tmp_path):
    store, tenant, manager, evidence, rid, doc, payload = integrated
    assert evidence.receive_maintenance_event(payload, manager.auth) is True
    assert counts(store) == (1, 1, 1)
    # Restart the receiver and replay with reordered JSON keys.
    reopened = build_evidence_service(store.path, str(tmp_path), StubDocumentClassifier())
    try:
        assert reopened.receive_maintenance_event(dict(reversed(list(payload.items()))), manager.auth) is True
        receipt = reopened.maintenance_receipt(payload["event_id"], "p1", manager.auth)
        assert receipt["status"] == "accepted" and receipt["received_by"] == "m1"
        assert receipt["payload"]["actor"] == "tenant:t1"  # audit, not receiver authorization
        timeline = reopened.maintenance_timeline(rid, "p1", manager.auth)
        assert len(timeline) == 1 and timeline[0]["event"]["evidence_refs"] == [doc]
        assert reopened.get_document(doc, "p1", tenant.auth).association.value == "associated"
    finally:
        reopened._repo.close()
    assert counts(store) == (1, 1, 1)


def test_concurrent_receivers_deduplicate(integrated, tmp_path):
    store, _, manager, evidence, _, _, payload = integrated
    other = build_evidence_service(store.path, str(tmp_path), StubDocumentClassifier())
    try:
        with ThreadPoolExecutor(2) as executor:
            results = list(executor.map(lambda svc: svc.receive_maintenance_event(payload, manager.auth),
                                        [evidence, other]))
        assert results == [True, True]
        assert counts(store) == (1, 1, 1)
    finally:
        other._repo.close()


@pytest.mark.parametrize("field,value", [("reason", "different"), ("evidence_refs", []),
                                         ("recipient_id", "tenant:other"), ("request_id", "other")])
def test_same_id_different_content_rejected(integrated, field, value):
    store, _, manager, evidence, _, _, payload = integrated
    assert evidence.receive_maintenance_event(payload, manager.auth)
    changed = {**payload, field: value}
    with pytest.raises(PermanentHandoffError) as exc:
        evidence.receive_maintenance_event(changed, manager.auth)
    assert exc.value.code == "event_id_conflict"
    assert counts(store) == (1, 1, 1)
    assert evidence.maintenance_receipt(payload["event_id"], "p1", manager.auth)["payload"] == payload


@pytest.mark.parametrize("refs", [["missing"], ["evidence/local.txt"], ["ev1", ""], ["other-property"]])
def test_invalid_reference_rejected_without_partial_writes(integrated, refs):
    store, _, manager, evidence, _, _, payload = integrated
    with store.transaction() as db:
        db.execute("INSERT INTO evidence_document "
                   "SELECT 'other-property','t2','p2',original_filename,file_type,size_bytes,storage_ref,"
                   "upload_status,created_at,updated_at FROM evidence_document WHERE document_id='ev1'")
    with pytest.raises(PermanentHandoffError) as exc:
        evidence.receive_maintenance_event({**payload, "evidence_refs": refs}, manager.auth)
    assert exc.value.code == "evidence_not_found"
    assert counts(store) == (0, 0, 0)


@pytest.mark.parametrize("auth", [AuthContext("t1", "p1"), AuthContext("t1", "p1", "m2"),
                                 AuthContext("t2", "p2", "m2")])
def test_actor_cannot_authorize_receiver(integrated, auth):
    store, _, _, evidence, _, _, payload = integrated
    with pytest.raises(PermanentHandoffError) as exc:
        evidence.receive_maintenance_event({**payload, "actor": "manager:m1"}, auth)
    assert exc.value.code == "forbidden"
    assert counts(store) == (0, 0, 0)


def test_transaction_rolls_back_receipt_timeline_and_all_links(integrated):
    store, _, manager, evidence, _, doc, payload = integrated
    with store.transaction() as db:
        db.execute("CREATE TRIGGER reject_second_link BEFORE INSERT ON evidence_event_document "
                   "WHEN NEW.document_id='ev1' BEGIN SELECT RAISE(ABORT,'injected failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        evidence.receive_maintenance_event({**payload, "evidence_refs": [doc, "ev1"]}, manager.auth)
    assert counts(store) == (0, 0, 0)
    assert evidence.maintenance_receipt(payload["event_id"], "p1", manager.auth)["status"] == "not_received"


def test_confirmed_busy_rollback_is_retryable(integrated):
    store, _, manager, evidence, _, _, payload = integrated
    evidence._repo._conn.execute("PRAGMA busy_timeout=1")
    with store.transaction():
        with pytest.raises(RetryableHandoffError):
            evidence.receive_maintenance_event(payload, manager.auth)
    assert counts(store) == (0, 0, 0)
    assert evidence.receive_maintenance_event(payload, manager.auth) is True


def test_tenant_reference_validation_enforces_ownership(integrated):
    store, tenant, manager, evidence, _, doc, _ = integrated
    assert evidence.validate_references([doc], "p1", tenant.auth)["status"] == "valid"
    with store.transaction() as db:
        db.execute("INSERT INTO tenant VALUES ('co-tenant','Other resident',NULL,'2026-01-01')")
        db.execute("INSERT INTO tenant_property (tenant_id,property_id,created_at) VALUES ('co-tenant','p1','2026-01-01')")
    with pytest.raises(EvidenceReferenceError):
        evidence.validate_references([doc], "p1", AuthContext("co-tenant", "p1"))
    with pytest.raises(PermissionError):
        evidence.validate_references([doc], "p1", AuthContext("t2", "p1"))
    with pytest.raises(PermissionError):
        evidence.validate_references([doc], "p1", AuthContext("t1", "p1", "m2"))
    assert evidence.validate_references([doc], "p1", manager.auth)["status"] == "valid"


@pytest.mark.parametrize("change", [{"event_id": ""}, {"occurred_at": "2026-10-24T12:00:00"},
                                    {"to_status": "invented"}, {"manager_id": "m1"},
                                    {"review_required": "yes"}])
def test_invalid_event_is_permanent_rejection(integrated, change):
    store, _, manager, evidence, _, _, payload = integrated
    with pytest.raises(PermanentHandoffError) as exc:
        evidence.receive_maintenance_event({**payload, **change}, manager.auth)
    assert exc.value.code == "invalid_event"
    assert counts(store) == (0, 0, 0)


def test_revoked_manager_cannot_replay_or_read_receipt(integrated):
    store, _, manager, evidence, _, _, payload = integrated
    assert evidence.receive_maintenance_event(payload, manager.auth)
    with store.transaction() as db:
        db.execute("DELETE FROM manager_property WHERE manager_id='m1'")
    with pytest.raises(PermanentHandoffError) as exc:
        evidence.receive_maintenance_event(payload, manager.auth)
    assert exc.value.code == "forbidden"
    with pytest.raises(PermissionError):
        evidence.maintenance_receipt(payload["event_id"], "p1", manager.auth)
    assert counts(store) == (1, 1, 1)


def test_storage_unavailable_cannot_close(integrated):
    store, _, manager, _, rid, doc, _ = integrated
    command(manager, rid, S.APPROVED, 2)
    command(manager, rid, S.IN_PROGRESS, 3)
    command(manager, rid, S.AWAITING_CONFIRMATION, 4, evidence_refs=[doc])
    with store.transaction() as db:
        db.execute("ALTER TABLE evidence_document RENAME TO evidence_document_unavailable")
    assert command(manager, rid, S.CLOSED, 5, evidence_refs=[doc]).code == "evidence_unavailable"
    assert manager.get(rid, "p1").status == S.AWAITING_CONFIRMATION


def test_dispatch_success_and_rejection(integrated):
    store, _, manager, evidence, _, _, _ = integrated
    callback = partial(evidence.receive_maintenance_event, auth=manager.auth)
    with store.transaction() as db:
        rows = db.execute("SELECT event_id,document FROM mc_outbox WHERE target='evidence' ORDER BY rowid").fetchall()
        bad = json.loads(rows[0]["document"])
        bad["evidence_refs"] = ["missing"]
        db.execute("UPDATE mc_outbox SET document=? WHERE event_id=? AND target='evidence'",
                   (json.dumps(bad), rows[0]["event_id"]))
    results = dispatch_pending(store, {"evidence": callback})
    assert [r["status"] for r in results] == ["rejected", "handed_off"]
    assert dispatch_pending(store, {"evidence": callback}) == []
    with store.transaction() as db:
        rejected = db.execute("SELECT status,last_error FROM mc_outbox WHERE event_id=? AND target='evidence'",
                              (rows[0]["event_id"],)).fetchone()
        assert tuple(rejected) == ("rejected", "evidence_not_found")


@pytest.mark.parametrize("commit_first", [False, True])
def test_timeout_receipt_reconciliation(integrated, commit_first):
    store, _, manager, evidence, _, _, _ = integrated
    delivered = []
    def timeout(payload):
        if commit_first:
            assert evidence.receive_maintenance_event(payload, manager.auth)
        delivered.append(payload)
        raise TimeoutError("test-only ambiguous outcome")

    result = dispatch_pending(store, {"evidence": timeout}, limit=1)[0]
    assert result["status"] == "uncertain"
    receipt = evidence.maintenance_receipt(result["event_id"], "p1", manager.auth)
    assert receipt["status"] == ("accepted" if commit_first else "not_received")
    if commit_first:
        assert receipt["payload"] == delivered[0]
    # The timed-out callback is finished before reconciling verified absence.
    reconcile(store, result["event_id"], "evidence", accepted=commit_first)
    callback = partial(evidence.receive_maintenance_event, auth=manager.auth)
    dispatch_pending(store, {"evidence": callback})
    with store.transaction() as db:
        assert db.execute("SELECT status FROM mc_outbox WHERE event_id=? AND target='evidence'",
                          (result["event_id"],)).fetchone()[0] == "handed_off"
    assert counts(store) == (2, 2, 0)


def test_dispatch_explicit_retry(integrated):
    store, _, manager, evidence, _, _, _ = integrated
    def busy(payload):
        raise RetryableHandoffError("confirmed not accepted")
    assert dispatch_pending(store, {"evidence": busy})[0]["status"] == "retry"
    assert counts(store) == (0, 0, 0)
    results = dispatch_pending(store, {"evidence": partial(evidence.receive_maintenance_event, auth=manager.auth)})
    assert all(r["status"] == "handed_off" for r in results)


def test_property_scoped_dispatch_does_not_reject_other_properties(integrated):
    store, tenant, manager, evidence, _, _, _ = integrated
    other = MaintenanceService(store, tenant.assessor, AuthContext("t2", "p2"))
    other.submit_issue(intake(tenant_id="t2", property_id="p2", message_id="other", event_id="other"))
    callback = partial(evidence.receive_maintenance_event, auth=manager.auth)
    results = dispatch_pending(store, {"evidence": callback}, property_id="p1")
    assert len(results) == 2 and all(r["status"] == "handed_off" for r in results)
    with store.transaction() as db:
        assert [r[0] for r in db.execute("SELECT status FROM mc_outbox WHERE property_id='p2' AND target='evidence'")] == ["pending", "pending"]
    other_callback = partial(evidence.receive_maintenance_event, auth=AuthContext("manager-session", "p2", "m2"))
    results = dispatch_pending(store, {"evidence": other_callback}, property_id="p2")
    assert len(results) == 2 and all(r["status"] == "handed_off" for r in results)
    assert counts(store) == (4, 4, 0)


@pytest.mark.parametrize("refs", [["missing"], ["other-property"], ["ev1", "missing"]])
def test_invalid_refs_cannot_enter_confirmation_or_close(integrated, refs):
    store, _, manager, _, rid, doc, _ = integrated
    with store.transaction() as db:
        db.execute("INSERT INTO evidence_document "
                   "SELECT 'other-property','t2','p2',original_filename,file_type,size_bytes,storage_ref,"
                   "upload_status,created_at,updated_at FROM evidence_document WHERE document_id='ev1'")
    assert command(manager, rid, S.APPROVED, 2).status == "applied"
    assert command(manager, rid, S.IN_PROGRESS, 3).status == "applied"
    def snapshot():
        with store.transaction() as db:
            return (db.execute("SELECT document FROM mc_request WHERE id=?", (rid,)).fetchone()[0],
                    db.execute("SELECT count(*) FROM mc_history").fetchone()[0],
                    db.execute("SELECT count(*) FROM mc_outbox").fetchone()[0])
    before = snapshot()
    assert command(manager, rid, S.AWAITING_CONFIRMATION, 4, evidence_refs=refs).code == "evidence_invalid"
    assert snapshot() == before
    assert command(manager, rid, S.AWAITING_CONFIRMATION, 4, evidence_refs=[doc]).status == "applied"
    before = snapshot()
    assert command(manager, rid, S.CLOSED, 5, evidence_refs=refs).code == "evidence_invalid"
    assert snapshot() == before
    assert manager.get(rid, "p1").status == S.AWAITING_CONFIRMATION
    assert command(manager, rid, S.CLOSED, 5, evidence_refs=[doc]).status == "applied"


def test_http_validation_receipt_and_scope(integrated, monkeypatch):
    store, tenant, manager, evidence, rid, doc, payload = integrated
    from main import create_app
    app = create_app(store.path, evidence._storage_root, classifier=StubDocumentClassifier())
    monkeypatch.setenv("RENTMATE_AUTH_TOKENS", json.dumps({
        "tenant": {"tenant_id": "t1", "property_id": "p1"},
        "manager": {"tenant_id": "manager-session", "property_id": "p1", "manager_id": "m1"},
        "other": {"tenant_id": "t2", "property_id": "p2", "manager_id": "m2"},
        "unbound": {"tenant_id": "t1", "property_id": "p1", "manager_id": "m2"},
    }))
    headers = lambda token: {"Authorization": "Bearer " + token}
    try:
        assert evidence.receive_maintenance_event(payload, manager.auth)
        with TestClient(app) as client:
            url = f"/api/evidence/maintenance-events/{payload['event_id']}"
            assert client.get(url, params={"property_id": "p1"}, headers=headers("manager")).json()["status"] == "accepted"
            for token in ("tenant", "unbound"):
                assert client.get(url, params={"property_id": "p1"}, headers=headers(token)).status_code == 403
            assert client.get(url, params={"property_id": "p2"}, headers=headers("other")).json()["status"] == "not_received"
            assert client.get(url, params={"property_id": "p1"}, headers=headers("other")).status_code == 403
            assert client.get(url, params={"property_id": "p1"}, headers=headers("unknown")).status_code == 401
            body = {"property_id": "p1", "evidence_refs": [doc]}
            assert client.post("/api/evidence/references/validate", json=body, headers=headers("tenant")).json()["status"] == "valid"
            assert client.post("/api/evidence/references/validate", json={**body, "manager_id": "m1"}, headers=headers("tenant")).status_code == 422
            assert client.post("/api/evidence/references/validate", json={**body, "evidence_refs": ["missing"]}, headers=headers("manager")).status_code == 404
            timeline = client.get("/api/evidence/maintenance-timeline", params={"property_id": "p1", "request_id": rid}, headers=headers("manager"))
            assert timeline.status_code == 200 and len(timeline.json()) == 1
    finally:
        app.state.evidence_service._repo.close()


def test_v3_database_upgrade_and_idempotency(tmp_path):
    path = tmp_path / "old.db"
    conn = init_connection(path)
    for table in ("evidence_event_document", "evidence_maintenance_timeline", "evidence_event_receipt"):
        conn.execute(f"DROP TABLE {table}")
    conn.execute("PRAGMA user_version=3")
    conn.commit()
    conn.close()
    for _ in range(2):
        upgraded = init_connection(path)
        try:
            assert upgraded.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 9
            assert upgraded.execute("SELECT count(*) FROM evidence_event_receipt").fetchone()[0] == 0
            assert upgraded.execute("PRAGMA foreign_key_check").fetchall() == []
        finally:
            upgraded.close()
