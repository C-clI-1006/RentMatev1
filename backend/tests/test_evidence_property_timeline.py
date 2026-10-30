"""UC6 using real Bearer resolution, property bindings and temporary SQLite only."""
from contextlib import closing
from datetime import datetime
import json
from pathlib import Path
import sqlite3

import pytest
from fastapi.testclient import TestClient

from llm.classify import ClassificationResult
from main import create_app
from scripts.seed_integrated_demo import seed
from shared.sqlite import init_connection, SCHEMA_VERSION


class InvoiceClassifier:
    source = "stub"

    def classify(self, **kwargs):
        return ClassificationResult(category="invoice", confidence=0.99,
                                    rationale="Explicit UC6 test classifier", source="stub", details={})


def headers(token="manager"):
    return {"Authorization": "Bearer " + token}


@pytest.fixture
def env(tmp_path, monkeypatch):
    path = tmp_path / "uc6.db"
    seed(path)
    monkeypatch.setenv("RENTMATE_AUTH_TOKENS", json.dumps({
        "manager": {"manager_id": "demo-manager", "property_id": "demo-property"},
        "tenant": {"tenant_id": "demo-tenant", "property_id": "demo-property"},
        "other": {"manager_id": "demo-manager-2", "property_id": "demo-property-2"},
        "forged": {"manager_id": "demo-manager-2", "property_id": "demo-property"},
    }))
    with closing(init_connection(path)) as db:
        db.execute("INSERT INTO lease(lease_id,property_id,tenant_id,start_date,created_at) "
                   "VALUES ('other-lease','demo-property-2','demo-tenant-2','2026-01-01','2026-01-01T00:00:00Z')")
        db.commit()
    app = create_app(str(path), str(tmp_path / "uploads"), classifier=InvoiceClassifier())
    with TestClient(app) as client:
        yield client, app.state.evidence_service, path
    app.state.evidence_service._repo.close()


def upload(client, token="manager", name="invoice.pdf"):
    content = (Path(__file__).resolve().parents[2] / "demo/samples/test-invoice.pdf").read_bytes()
    r = client.post("/api/evidence/documents", headers=headers(token),
                    data={"property_id": "demo-property"}, files={"file": (name, content, "application/pdf")})
    assert r.status_code == 200 and r.json()["status"] == "accepted"
    return r.json()["document"]["document_id"]


def link(client, did, target="demo-lease", kind="lease", token="manager", **extra):
    return client.post(f"/api/evidence/documents/{did}/links", headers=headers(token),
                       json={"property_id": "demo-property", "target_type": kind, "target_id": target, **extra})


def timeline(client, did=None, token="manager", property_id="demo-property"):
    params = {"property_id": property_id}
    if did is not None:
        params["document_id"] = did
    return client.get("/api/evidence/timeline", params=params, headers=headers(token))


def test_upload_appears_without_maintenance_or_manual_link(env):
    client, _, _ = env
    did = upload(client)
    entries = timeline(client, did).json()
    assert [r["kind"] for r in entries] == ["uploaded", "classification"]
    assert entries[0]["actor"] == "manager:demo-manager"
    assert entries[1]["category"] == "invoice" and entries[1]["source"] == "stub"
    assert len({r["entry_id"] for r in entries}) == 2
    assert client.get(f"/api/evidence/documents/{did}", params={"property_id": "demo-property"}, headers=headers()).json()["association"] == "unassociated"


def test_manual_lease_link_is_durable_idempotent_and_does_not_modify_lease(env, tmp_path):
    client, _, path = env
    did = upload(client, "tenant")
    with closing(init_connection(path)) as db:
        before = tuple(db.execute("SELECT * FROM lease WHERE lease_id='demo-lease'").fetchone())
    first = link(client, did)
    assert first.status_code == 200 and first.json()["created"] is True
    assert first.json()["associated_by"] == "demo-manager"
    repeated = link(client, did).json()
    assert repeated["created"] is False and repeated["association_id"] == first.json()["association_id"]
    for token in ["manager", "tenant"]:
        listed = client.get("/api/evidence/documents", params={"property_id": "demo-property"}, headers=headers(token)).json()
        assert next(d for d in listed if d["document_id"] == did)["association"] == "associated"
    with closing(init_connection(path)) as db:
        assert tuple(db.execute("SELECT * FROM lease WHERE lease_id='demo-lease'").fetchone()) == before
    reopened = create_app(str(path), str(tmp_path / "reopened"), classifier=InvoiceClassifier())
    try:
        with TestClient(reopened) as again:
            rows = timeline(again, did).json()
            assert sum(r["kind"] == "linked" for r in rows) == 1
            assert link(again, did).json()["created"] is False
    finally:
        reopened.state.evidence_service._repo.close()


def test_multiple_documents_per_record_and_multiple_record_types(env):
    client, _, path = env
    a, b = upload(client), upload(client)
    assert link(client, a).status_code == link(client, b).status_code == 200
    request = client.post("/api/maintenance/requests", headers=headers("tenant"), json={
        "title": "UC6 link test", "description": "Loose handle, no immediate danger", "evidence_refs": []}).json()
    rid = request["request_id"]
    with closing(init_connection(path)) as db:
        before = tuple(db.execute("SELECT * FROM mc_request WHERE id=?", (rid,)).fetchone())
    assert link(client, a, rid, "maintenance_request").status_code == 200
    links = client.get(f"/api/evidence/documents/{a}/links", headers=headers(), params={"property_id": "demo-property"}).json()
    assert {r["target_type"] for r in links} == {"lease", "maintenance_request"}
    with closing(init_connection(path)) as db:
        assert tuple(db.execute("SELECT * FROM mc_request WHERE id=?", (rid,)).fetchone()) == before
    targets = client.get("/api/evidence/link-targets", headers=headers(), params={"property_id": "demo-property"}).json()
    assert any(t["target_id"] == rid for t in targets)
    assert any(t["target_id"] == "demo-lease" for t in targets)
    assert not any(t["target_id"] == "other-lease" for t in targets)


@pytest.mark.parametrize("target", ["missing", "other-lease", "evidence/local.pdf"])
def test_missing_and_cross_property_targets_rejected_without_writes(env, target):
    client, _, path = env
    did = upload(client)
    before = timeline(client, did).json()
    r = link(client, did, target)
    assert r.status_code == 404 and r.json()["detail"]["code"] == "related_record_not_found"
    assert timeline(client, did).json() == before
    with closing(init_connection(path)) as db:
        assert db.execute("SELECT count(*) FROM evidence_association").fetchone()[0] == 0


@pytest.mark.parametrize("token", ["tenant", "other", "forged", "unknown"])
def test_manager_only_link_and_timeline_permissions(env, token):
    client, _, _ = env
    did = upload(client)
    expected = 401 if token == "unknown" else 403
    assert link(client, did, token=token).status_code == expected
    assert timeline(client, did, token=token).status_code == expected
    assert client.get("/api/evidence/link-targets", params={"property_id": "demo-property"}, headers=headers(token)).status_code == expected
    assert client.get(f"/api/evidence/documents/{did}/links", params={"property_id": "demo-property"}, headers=headers(token)).status_code == expected


def test_unknown_or_cross_property_documents_are_concealed(env):
    client, _, _ = env
    did = upload(client)
    assert timeline(client, did, "other", "demo-property-2").status_code == 404
    assert link(client, "missing").status_code == 404
    assert link(client, did, associated_by="forged").status_code == 422
    assert link(client, did, kind="property").status_code == 422


@pytest.mark.parametrize("status,category,flags", [("failed", None, {}), ("classified", "unknown", {"review_required": True}),
                                                   ("classified", "invoice", {"review_required": True}), ("pending", None, {})])
def test_uncertain_classification_requires_review_before_linking(env, status, category, flags):
    client, _, path = env
    did = upload(client)
    with closing(init_connection(path)) as db:
        db.execute("UPDATE evidence_classification SET status=?,classification_type=?,details=? WHERE document_id=?",
                   (status, category, json.dumps(flags), did))
        db.commit()
    assert link(client, did).status_code == 409
    if status == "pending":
        return
    r = client.post("/api/evidence/reviews", headers=headers(), json={"status": "select_category", "document_id": did,
                    "property_id": "demo-property", "category": "invoice"})
    assert r.status_code == 200
    assert link(client, did).status_code == 200
    with closing(init_connection(path)) as db:
        row = db.execute("SELECT status,classification_type FROM evidence_classification WHERE document_id=?", (did,)).fetchone()
        assert tuple(row) == (status, category)


def test_review_history_and_timezones_have_stable_chronological_order(env):
    client, _, path = env
    did = upload(client)
    for category in ["lease_document", "invoice"]:
        assert client.post("/api/evidence/reviews", headers=headers(), json={"status": "select_category", "document_id": did,
                           "property_id": "demo-property", "category": category}).status_code == 200
    with closing(init_connection(path)) as db:
        db.execute("UPDATE evidence_document SET created_at='2026-10-30T10:30:00+11:00' WHERE document_id=?", (did,))
        db.execute("UPDATE evidence_classification SET classified_at='2026-10-30T00:00:00Z' WHERE document_id=?", (did,))
        db.commit()
    rows = timeline(client, did).json()
    assert rows[0]["kind"] == "uploaded" and rows[1]["kind"] == "classification"
    assert [r["category"] for r in rows if r["kind"] == "reviewed"] == ["lease_document", "invoice"]
    assert timeline(client, did).json() == rows


def test_revoked_binding_and_sqlite_failure_do_not_create_links(env):
    client, _, path = env
    did = upload(client)
    with closing(init_connection(path)) as db:
        db.execute("CREATE TRIGGER fail_link BEFORE INSERT ON evidence_association BEGIN SELECT RAISE(ABORT,'test failure'); END")
        db.commit()
    assert link(client, did).status_code == 503
    assert not any(e["kind"] == "linked" for e in timeline(client, did).json())
    with closing(init_connection(path)) as db:
        assert db.execute("SELECT count(*) FROM evidence_association").fetchone()[0] == 0
        db.execute("DELETE FROM manager_property WHERE manager_id='demo-manager'")
        db.commit()
    assert link(client, did).status_code == timeline(client, did).status_code == 403


def test_v11_upgrade_preserves_links_and_replaces_target_uniqueness(env):
    client, _, path = env
    a, b = upload(client), upload(client)
    assert link(client, a).status_code == 200
    with closing(sqlite3.connect(path)) as db:
        db.execute("ALTER TABLE evidence_association DROP COLUMN associated_by")
        db.execute("DROP INDEX idx_evidence_association_target")
        db.execute("CREATE UNIQUE INDEX idx_evidence_association_target ON evidence_association(target_type,target_id)")
        db.execute("CREATE INDEX custom_link_index ON evidence_association(created_at)")
        db.execute("PRAGMA user_version=11")
        db.commit()
    for _ in range(2):
        with closing(init_connection(path)) as db:
            assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 12
            old = db.execute("SELECT * FROM evidence_association WHERE document_id=?", (a,)).fetchone()
            assert old["target_id"] == "demo-lease" and old["associated_by"] is None
            assert db.execute("SELECT 1 FROM sqlite_master WHERE name='custom_link_index'").fetchone()
            assert not db.execute("PRAGMA foreign_key_check").fetchall()
    assert link(client, b).status_code == 200


def test_v12_migration_failure_rolls_back_column_and_index(env):
    _, _, path = env
    from shared.sqlite import _ensure_v12_evidence_record_links
    with closing(sqlite3.connect(path)) as db:
        db.execute("ALTER TABLE evidence_association DROP COLUMN associated_by")
        db.execute("DROP INDEX idx_evidence_association_target")
        db.execute("CREATE UNIQUE INDEX idx_evidence_association_target ON evidence_association(target_type,target_id)")
        db.execute("PRAGMA user_version=11")
        db.commit()
        db.set_authorizer(lambda action, a, b, c, d: sqlite3.SQLITE_DENY
                          if action == sqlite3.SQLITE_CREATE_INDEX and a == "idx_evidence_association_target" else sqlite3.SQLITE_OK)
        with pytest.raises(sqlite3.DatabaseError):
            _ensure_v12_evidence_record_links(db)
        db.set_authorizer(None)
        assert "associated_by" not in {r[1] for r in db.execute("PRAGMA table_info(evidence_association)")}
        assert "document_id" not in db.execute("SELECT sql FROM sqlite_master WHERE name='idx_evidence_association_target'").fetchone()[0]
        assert db.execute("PRAGMA user_version").fetchone()[0] == 11
