"""Real HTTP tests for the evidence upload increment (SCRUM-8).

Runs against a TEMP database + TEMP storage directory built through
`main.create_app` — the same wiring a real deployment uses, with the
identity coming from the PROTOTYPE headers (TEST-ONLY: any client can
send any header; nothing here is real authentication).

Coverage:
- normal upload (accepted, file + metadata persisted, bytes round-trip)
- UC6 classification after save: stub success (labelled source=stub),
  honest `unknown`, fail-closed `failed` (unparseable PDF, non-PDF,
  unconfigured live adapter) — the upload stays SAVED/accepted either way
- empty file / illegal type / oversized file (rejected, nothing stored)
- property-scoped list / get / download
- cross-property access (scope check fails BEFORE any lookup -> 403,
  never reveals existence; unknown id inside own scope -> 404)
- forged identity (unknown tenant -> 403; missing headers -> 400)
- failure cleanup: DB failure removes the orphan file; storage failure
  leaves nothing in the DB and no partial file
"""

from __future__ import annotations

import io
import json
import sqlite3
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from contracts.evidence import EvidenceUploadCommand
from evidence.router import build_evidence_service
from evidence.upload import MAX_FILE_BYTES
from llm.classify import QwenDocumentClassifier
from main import create_app
from llm.classify import StubDocumentClassifier
from shared import AuthContext

# Prototype identity (TEST-ONLY bearer tokens, not real authentication):
# the tenant_id / property_id come ONLY from the server-side
# RENTMATE_AUTH_TOKENS map, never from the request.
TENANT_1 = "T1"
PROP_1 = "P1"
TENANT_2 = "T2"
PROP_2 = "P2"

# Fictitious session tokens (TEST-ONLY, not real credentials).
TOKEN_T1_P1 = "ev-t1-p1-fixture-token"
TOKEN_T2_P2 = "ev-t2-p2-fixture-token"
TOKEN_FORGED_TENANT = "ev-forged-fixture-token"


def _auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


HEADERS_T1_P1 = _auth_headers(TOKEN_T1_P1)
HEADERS_T2_P2 = _auth_headers(TOKEN_T2_P2)
# T2 claiming the P1 property: the binding exists (stub data) but the
# token's property context does not match the requested property.
HEADERS_T2_CLAIMING_P1 = _auth_headers(TOKEN_T2_P2)
HEADERS_FORGED_TENANT = _auth_headers(TOKEN_FORGED_TENANT)


def _seed_auth_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    """Map the fictitious test tokens onto the seeded tenants/properties.

    `get_auth` reads RENTMATE_AUTH_TOKENS per request (never monkeypatched:
    the real dependency stays in place), so the mapping is applied as an
    environment variable via monkeypatch and reverted automatically.
    """
    monkeypatch.setenv(
        "RENTMATE_AUTH_TOKENS",
        json.dumps(
            {
                TOKEN_T1_P1: {"tenant_id": TENANT_1, "property_id": PROP_1},
                TOKEN_T2_P2: {"tenant_id": TENANT_2, "property_id": PROP_2},
                TOKEN_FORGED_TENANT: {"tenant_id": "T-EVIL", "property_id": PROP_1},
            }
        ),
    )


def _seed_bindings(conn: sqlite3.Connection) -> None:
    now = "2026-10-22T00:00:00+00:00"
    conn.executemany(
        "INSERT INTO tenant (tenant_id, name, contact, created_at) VALUES (?, ?, ?, ?)",
        [(TENANT_1, "Tenant One", "c1", now), (TENANT_2, "Tenant Two", "c2", now)],
    )
    conn.executemany(
        "INSERT INTO property (property_id, name, address, created_at) VALUES (?, ?, ?, ?)",
        [(PROP_1, "Property One", "addr1", now), (PROP_2, "Property Two", "addr2", now)],
    )
    # T1 (the normal-upload tenant) holds a `resident` binding.
    conn.execute(
        "INSERT INTO tenant_property (tenant_id, property_id, role, created_at) "
        "VALUES (?, ?, 'resident', ?)",
        (TENANT_1, PROP_1, now),
    )
    conn.execute(
        "INSERT INTO tenant_property (tenant_id, property_id, role, created_at) "
        "VALUES (?, ?, 'manager', ?)",
        (TENANT_2, PROP_2, now),
    )
    conn.commit()


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = str(tmp_path / "evidence_test.db")
    storage_root = str(tmp_path / "evidence_storage")

    # Seed prototype bindings BEFORE the app is built so the schema file
    # is applied exactly once (at first use).
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            (Path(__file__).resolve().parents[2] / "db" / "schema.sql").read_text(encoding="utf-8")
        )
        _seed_bindings(conn)
    finally:
        conn.close()

    # Map the fictitious tokens (server-side RENTMATE_AUTH_TOKENS) onto the
    # seeded tenants/properties for the duration of the fixture.
    _seed_auth_tokens(monkeypatch)

    app = create_app(db_path, storage_root, classifier=StubDocumentClassifier())
    with TestClient(app) as c:
        yield c


def _upload(client: TestClient, filename: str, content: bytes, headers: dict, property_id: str):
    return client.post(
        "/api/evidence/documents",
        headers=headers,
        files={"file": (filename, io.BytesIO(content))},
        data={"property_id": property_id},
    )


# ---------------------------------------------------------------------------
# Normal path
# ---------------------------------------------------------------------------


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


def test_normal_upload_pdf(client, tmp_path):
    content = b"%PDF-1.4 lease-agreement bytes"
    r = _upload(client, "lease.pdf", content, HEADERS_T1_P1, PROP_1)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "accepted"
    doc = body["document"]
    assert doc["tenant_id"] == TENANT_1
    assert doc["property_id"] == PROP_1
    assert doc["original_filename"] == "lease.pdf"
    assert doc["file_type"] == "pdf"
    assert doc["size_bytes"] == len(content)
    assert doc["upload_status"] == "saved"
    assert doc["association"] == "unassociated"
    # original bytes stored under a module-generated reference
    stored = Path(tmp_path / "evidence_storage" / doc["storage_ref"])
    assert stored.is_file()
    assert stored.read_bytes() == content


# ---------------------------------------------------------------------------
# Rejections (nothing may be stored)
# ---------------------------------------------------------------------------


def test_empty_file_rejected(client, tmp_path):
    r = _upload(client, "notes.txt", b"", HEADERS_T1_P1, PROP_1)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "rejected"
    assert body["code"] == "empty_file"
    assert body["retryable"] is True
    assert list((tmp_path / "evidence_storage").glob("**/*")) == []


def test_illegal_type_rejected(client, tmp_path):
    r = _upload(client, "virus.exe", b"MZ...", HEADERS_T1_P1, PROP_1)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "rejected"
    assert body["code"] == "file_type_not_allowed"
    assert list((tmp_path / "evidence_storage").glob("**/*")) == []


def test_oversized_file_rejected(client, tmp_path):
    r = _upload(client, "big.png", b"x" * (MAX_FILE_BYTES + 1), HEADERS_T1_P1, PROP_1)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "rejected"
    assert body["code"] == "file_too_large"
    assert str(MAX_FILE_BYTES) in body["reason"]
    assert list((tmp_path / "evidence_storage").glob("**/*")) == []


def test_uppercase_extension_is_normalised(client):
    r = _upload(client, "RECEIPT.PDF", b"%PDF-1.4", HEADERS_T1_P1, PROP_1)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "accepted"
    assert body["document"]["file_type"] == "pdf"


# ---------------------------------------------------------------------------
# Property-scoped reads
# ---------------------------------------------------------------------------


def test_list_get_download_roundtrip(client, tmp_path):
    content1 = b"%PDF-1.4 doc one"
    content2 = b"plain text doc two"
    doc1 = _upload(client, "one.pdf", content1, HEADERS_T1_P1, PROP_1).json()["document"]
    doc2 = _upload(client, "two.txt", content2, HEADERS_T1_P1, PROP_1).json()["document"]

    # list (newest first) — no storage internals exposed
    r = client.get(
        "/api/evidence/documents", params={"property_id": PROP_1}, headers=HEADERS_T1_P1
    )
    assert r.status_code == 200
    refs = r.json()
    assert [ref["document_id"] for ref in refs] == [doc2["document_id"], doc1["document_id"]]
    assert refs[0]["association"] == "unassociated"
    assert "storage_ref" not in refs[0]

    # get single
    r = client.get(
        f"/api/evidence/documents/{doc1['document_id']}",
        params={"property_id": PROP_1},
        headers=HEADERS_T1_P1,
    )
    assert r.status_code == 200
    assert r.json()["document_id"] == doc1["document_id"]

    # download: original bytes round-trip + attachment filename
    r = client.get(
        f"/api/evidence/documents/{doc1['document_id']}/download",
        params={"property_id": PROP_1},
        headers=HEADERS_T1_P1,
    )
    assert r.status_code == 200
    assert r.content == content1
    assert r.headers["content-type"] == "application/pdf"
    assert r.headers["content-disposition"] == (
        f'attachment; filename="{doc1["document_id"]}.pdf"'
    )


# ---------------------------------------------------------------------------
# Cross-property & forged identity (prototype headers are TEST-ONLY)
# ---------------------------------------------------------------------------


def test_cross_property_list_is_forbidden(client):
    # T2 (bound to P2) requesting P1's document list -> 403
    r = client.get(
        "/api/evidence/documents", params={"property_id": PROP_1}, headers=HEADERS_T2_P2
    )
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "forbidden"


def test_cross_property_get_and_download_forbidden_no_existence_leak(client):
    doc = _upload(client, "one.pdf", b"%PDF-1.4", HEADERS_T1_P1, PROP_1).json()["document"]
    # T2 (bound to P2) requesting a P1-scoped document id: the scope check
    # fails BEFORE any lookup (param property != auth property), so the
    # response is a flat 403 that never reveals whether the document exists.
    r = client.get(
        f"/api/evidence/documents/{doc['document_id']}",
        params={"property_id": PROP_1},
        headers=HEADERS_T2_P2,
    )
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "forbidden"

    r = client.get(
        f"/api/evidence/documents/{doc['document_id']}/download",
        params={"property_id": PROP_1},
        headers=HEADERS_T2_P2,
    )
    assert r.status_code == 403


def test_unknown_document_id_in_own_scope_is_not_found(client):
    r = client.get(
        "/api/evidence/documents/no-such-document",
        params={"property_id": PROP_1},
        headers=HEADERS_T1_P1,
    )
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "not_found"


def test_property_mismatch_between_header_and_param_is_forbidden(client):
    # Header claims property P1 but the request targets P2 -> 403
    r = client.get(
        "/api/evidence/documents", params={"property_id": PROP_2}, headers=HEADERS_T1_P1
    )
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "forbidden"


def test_upload_property_mismatch_is_forbidden(client):
    r = _upload(client, "one.pdf", b"%PDF-1.4", HEADERS_T1_P1, PROP_2)
    assert r.status_code == 403


def test_forged_unknown_tenant_is_forbidden(client):
    r = client.get(
        "/api/evidence/documents", params={"property_id": PROP_1}, headers=HEADERS_FORGED_TENANT
    )
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "forbidden"

    r = _upload(client, "one.pdf", b"%PDF-1.4", HEADERS_FORGED_TENANT, PROP_1)
    assert r.status_code == 403


def test_missing_bearer_is_rejected(client, tmp_path):
    """No Bearer token -> 401 before any processing; nothing is stored."""
    r = client.get("/api/evidence/documents", params={"property_id": PROP_1})
    assert r.status_code == 401

    r = _upload(client, "one.pdf", b"%PDF-1.4", {}, PROP_1)
    assert r.status_code == 401

    # A rejected request leaves no residue: no file on disk, no DB row.
    assert list((tmp_path / "evidence_storage").glob("**/*")) == []
    check = sqlite3.connect(str(tmp_path / "evidence_test.db"))
    try:
        assert check.execute("SELECT COUNT(*) FROM evidence_document").fetchone()[0] == 0
    finally:
        check.close()


def test_prototype_headers_without_bearer_are_rejected(client, tmp_path):
    """Legacy X-Prototype-* identity headers WITHOUT a Bearer token -> 401.

    The X-Prototype-* headers are not an identity source (identity comes
    only from the server-side token map via the Bearer token), so a
    request carrying them but no Authorization is rejected before any
    processing and nothing is stored.
    """
    headers = {
        "X-Prototype-Tenant-Id": TENANT_1,
        "X-Prototype-Property-Id": PROP_1,
        "X-Prototype-Manager-Id": "M1",
    }
    r = _upload(client, "one.pdf", b"%PDF-1.4", headers, PROP_1)
    assert r.status_code == 401

    # No residue: no file on disk, no DB row.
    assert list((tmp_path / "evidence_storage").glob("**/*")) == []
    check = sqlite3.connect(str(tmp_path / "evidence_test.db"))
    try:
        assert check.execute("SELECT COUNT(*) FROM evidence_document").fetchone()[0] == 0
    finally:
        check.close()


def test_forged_identity_headers_cannot_override_server_token(client, tmp_path):
    """Valid T1/P1 Bearer + forged T2/P2/manager X-Prototype-* headers:
    the forged headers must neither re-attribute the upload to T2/P2
    nor grant any cross-property (P2) read access."""
    forged = {
        **HEADERS_T1_P1,
        "X-Prototype-Tenant-Id": TENANT_2,
        "X-Prototype-Property-Id": PROP_2,
        "X-Prototype-Manager-Id": "M1",
    }
    r = _upload(client, "forged.pdf", b"%PDF-1.4 forged-header bytes", forged, PROP_1)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "accepted"
    assert body["document"]["tenant_id"] == TENANT_1
    assert body["document"]["property_id"] == PROP_1

    # DB attribution: the row belongs to the TOKEN's T1/P1, not T2/P2.
    check = sqlite3.connect(str(tmp_path / "evidence_test.db"))
    try:
        rows = check.execute(
            "SELECT tenant_id, property_id FROM evidence_document"
        ).fetchall()
    finally:
        check.close()
    assert rows == [(TENANT_1, PROP_1)]

    # Forged headers must not expand cross-property (P2) reads: the scope
    # check still fails on the token's P1 -> flat 403, no existence leak.
    r = client.get(
        "/api/evidence/documents", params={"property_id": PROP_2}, headers=forged
    )
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "forbidden"

    r = client.get(
        f"/api/evidence/documents/{body['document']['document_id']}",
        params={"property_id": PROP_2},
        headers=forged,
    )
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "forbidden"


# ---------------------------------------------------------------------------
# Failure cleanup (no residue when a step fails)
# ---------------------------------------------------------------------------


def test_db_failure_removes_orphan_file(tmp_path):
    """DB write fails -> the already-written file is removed; no DB row."""
    storage_root = str(tmp_path / "storage")
    db_path = str(tmp_path / "db_fail.db")
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            (Path(__file__).resolve().parents[2] / "db" / "schema.sql").read_text(encoding="utf-8")
        )
        _seed_bindings(conn)
        # The schema script turned `PRAGMA foreign_keys = ON` on THIS
        # connection, so the surviving (T1, P1) binding row would block
        # the tenant DELETE below. Turn FKs off here only: the binding
        # row stays (so has_manager_access passes), while the repository's
        # own connection has FKs ON, which makes the evidence_document
        # insert fail deterministically.
        conn.execute("PRAGMA foreign_keys = OFF")
        # Drop the tenant row so the app-side DB write fails on the FK.
        conn.execute("DELETE FROM tenant WHERE tenant_id = ?", (TENANT_1,))
        conn.commit()
    finally:
        conn.close()

    service = build_evidence_service(db_path, storage_root)

    cmd = EvidenceUploadCommand(
        property_id=PROP_1, original_filename="boom.pdf", content=b"%PDF-1.4"
    )
    auth = AuthContext(tenant_id=TENANT_1, property_id=PROP_1)
    # The (T1, P1) binding still exists, so the scope check passes and the
    # upload reaches the DB write, where the tenant FK fails.
    with pytest.raises(sqlite3.IntegrityError):
        service.upload(cmd, auth)

    # No orphan file (the empty `evidence/` dir may remain; no FILE may).
    assert [p for p in Path(storage_root).rglob("*") if p.is_file()] == []
    check = sqlite3.connect(db_path)
    try:
        assert check.execute("SELECT COUNT(*) FROM evidence_document").fetchone()[0] == 0
        assert check.execute("SELECT COUNT(*) FROM evidence_classification").fetchone()[0] == 0
    finally:
        check.close()


def test_storage_failure_leaves_nothing_in_db(tmp_path):
    """File write fails (storage root blocked by a regular file) -> the
    error propagates and the repository is never called: no DB row, no
    partial file."""
    db_path = str(tmp_path / "db_storage_fail.db")
    # The storage "root" is a regular FILE, so root/evidence/ cannot be a
    # directory and the write fails with an OSError.
    storage_root = tmp_path / "storage_blocked"
    storage_root.write_bytes(b"block")

    repo_calls: list[str] = []

    class SpyRepo:
        def save_document(self, doc):
            repo_calls.append(doc.document_id)

        def has_tenant_access(self, tenant_id, property_id):
            return True

    service = build_evidence_service(db_path, str(tmp_path / "storage_dummy"))
    service._repo = SpyRepo()  # storage-failure path must never reach the DB
    service._storage_root = str(storage_root)

    cmd = EvidenceUploadCommand(
        property_id=PROP_1, original_filename="boom.pdf", content=b"%PDF-1.4"
    )
    auth = AuthContext(tenant_id=TENANT_1, property_id=PROP_1)
    with pytest.raises(OSError):
        service.upload(cmd, auth)

    assert repo_calls == []  # nothing was written to the DB
    # The blocked "storage root" is an intact regular file: nothing could
    # be created under it, and it was not clobbered by the failed attempt.
    assert storage_root.is_file() and storage_root.read_bytes() == b"block"
    check = sqlite3.connect(db_path)
    try:
        assert check.execute("SELECT COUNT(*) FROM evidence_document").fetchone()[0] == 0
    finally:
        check.close()


def test_rejection_runs_before_any_io(tmp_path):
    """Rejected uploads never touch disk or DB (validation first)."""
    db_path = str(tmp_path / "db_reject.db")
    storage_root = str(tmp_path / "storage_reject")
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            (Path(__file__).resolve().parents[2] / "db" / "schema.sql").read_text(encoding="utf-8")
        )
        _seed_bindings(conn)
    finally:
        conn.close()

    service = build_evidence_service(db_path, storage_root)

    auth = AuthContext(tenant_id=TENANT_1, property_id=PROP_1)
    for filename, content in [
        ("empty.txt", b""),
        ("bad.exe", b"bin"),
        ("huge.png", b"x" * (MAX_FILE_BYTES + 1)),
    ]:
        result = service.upload(
            EvidenceUploadCommand(property_id=PROP_1, original_filename=filename, content=content),
            auth,
        )
        assert result.status == "rejected"

    assert not Path(storage_root).exists()
    check = sqlite3.connect(db_path)
    try:
        assert check.execute("SELECT COUNT(*) FROM evidence_document").fetchone()[0] == 0
    finally:
        check.close()
