"""HTTP review regressions: real Bearer authentication and manager_property.

Independent temporary DB checks retain machine outcomes and review history.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from main import create_app
from llm.classify import StubDocumentClassifier

# Prototype identity (TEST-ONLY headers, not real authentication).
TENANT_1 = "T1"
PROP_1 = "P1"
MANAGER_9 = "mgr-9"  # verified through manager_property; becomes reviewed_by

NOW = "2026-10-23T00:00:00+00:00"
DOC_1 = "doc-ev-rtr-select-1"
DOC_2 = "doc-ev-rtr-confirm-1"
DOC_3 = "doc-ev-rtr-confirm-reject-1"
DOC_MISSING = "doc-ev-rtr-missing-1"  # never seeded: in-scope 404 case

# A second tenant + property with NO `tenant_property` binding (seeded
# per-test where needed) — the "no manager binding" 403 case.
TENANT_2 = "T2"
PROP_2 = "P2"

# The machine's settled suggestion (status `classified`) that the
# manager is about to confirm.
MACHINE_CATEGORY = "lease_document"
CLASSIFIED_DETAILS_JSON = json.dumps(
    {"rationale": "lease agreement text detected", "review_required": True}
)

HEADERS_T1_P1 = {"Authorization": "Bearer test-review-manager"}
TENANT_HEADERS = {"Authorization": "Bearer test-review-tenant"}


# The machine's FAILED outcome awaiting manual review: a `failed`
# status with a NULL category and review_required=True in details.
FAILED_DETAILS_JSON = json.dumps(
    {
        "code": "pdf_no_text",
        "reason": "pages exist but no extractable text",
        "review_required": True,
    }
)


def _seed_bindings(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO tenant (tenant_id, name, contact, created_at) VALUES (?, ?, ?, ?)",
        (TENANT_1, "Tenant One", "c1", NOW),
    )
    conn.execute(
        "INSERT INTO property (property_id, name, address, created_at) "
        "VALUES (?, ?, ?, ?)",
        (PROP_1, "Property One", "addr1", NOW),
    )
    conn.execute(
        "INSERT INTO tenant_property (tenant_id, property_id, role, created_at) "
        "VALUES (?, ?, 'resident', ?)",
        (TENANT_1, PROP_1, NOW),
    )
    conn.execute("INSERT INTO manager VALUES (?, ?, ?)", (MANAGER_9, "Independent Manager", NOW))
    conn.execute("INSERT INTO manager_property VALUES (?, ?, ?)", (MANAGER_9, PROP_1, NOW))
    conn.execute("INSERT INTO manager VALUES (?, ?, ?)", ("unbound-manager", "Unbound Manager", NOW))
    conn.commit()


def _seed_failed_doc(conn: sqlite3.Connection, document_id: str) -> None:
    """Seed a stored document whose machine classification FAILED and
    is flagged review_required — the case `select_category` exists for
    (the manager cannot confirm a failed suggestion, but CAN pick the
    final category)."""
    _seed_document(conn, document_id)
    conn.execute(
        "INSERT INTO evidence_classification (document_id, status, source, "
        "classification_type, confidence, details, created_at, classified_at) "
        "VALUES (?, 'failed', 'stub', NULL, NULL, ?, ?, ?)",
        (document_id, FAILED_DETAILS_JSON, NOW, NOW),
    )
    conn.commit()


def _seed_classified_doc(conn: sqlite3.Connection, document_id: str) -> None:
    """Seed a stored document whose machine classification SUCCEEDED
    (`classified`, a valid business category) and is flagged
    review_required — the case `confirm_suggested` can settle."""
    _seed_document(conn, document_id)
    conn.execute(
        "INSERT INTO evidence_classification (document_id, status, source, "
        "classification_type, confidence, details, created_at, classified_at) "
        "VALUES (?, 'classified', 'stub', ?, ?, ?, ?, ?)",
        (document_id, MACHINE_CATEGORY, 0.8, CLASSIFIED_DETAILS_JSON, NOW, NOW),
    )
    conn.commit()


def _seed_document(conn: sqlite3.Connection, document_id: str) -> None:
    """Seed the `evidence_document` row shared by all seeded docs."""
    conn.execute(
        "INSERT INTO evidence_document (document_id, tenant_id, property_id, "
        "original_filename, file_type, size_bytes, storage_ref, upload_status, "
        "created_at, updated_at) VALUES (?, ?, ?, 'lease.pdf', 'pdf', 10, "
        "'evidence/x.pdf', 'saved', ?, ?)",
        (document_id, TENANT_1, PROP_1, NOW, NOW),
    )


def _seed_unbound_tenant_property(conn: sqlite3.Connection) -> None:
    """Seed a second tenant + property pair with NO `tenant_property`
    binding row: the pair exists in the tenant/property tables, so the
    only scope failure left is the missing manager binding."""
    conn.execute(
        "INSERT INTO tenant (tenant_id, name, contact, created_at) VALUES (?, ?, ?, ?)",
        (TENANT_2, "Tenant Two", "c2", NOW),
    )
    conn.execute(
        "INSERT INTO property (property_id, name, address, created_at) "
        "VALUES (?, ?, ?, ?)",
        (PROP_2, "Property Two", "addr2", NOW),
    )
    conn.commit()


@pytest.fixture()
def client(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("RENTMATE_AUTH_TOKENS", json.dumps({
        "test-review-manager": {"tenant_id": "manager-session", "property_id": PROP_1, "manager_id": MANAGER_9},
        "test-review-tenant": {"tenant_id": TENANT_1, "property_id": PROP_1},
        "test-review-unbound": {"tenant_id": TENANT_2, "property_id": PROP_2, "manager_id": "unbound-manager"},
    }))
    db_path = str(tmp_path / "evidence_review_router.db")
    storage_root = str(tmp_path / "evidence_storage")

    # Seed verified bindings + the review docs BEFORE the app is built
    # so the schema file is applied exactly once (at first use).
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            (Path(__file__).resolve().parents[2] / "db" / "schema.sql").read_text(encoding="utf-8")
        )
        _seed_bindings(conn)
        _seed_unbound_tenant_property(conn)
        _seed_failed_doc(conn, DOC_1)
        _seed_classified_doc(conn, DOC_2)
        _seed_failed_doc(conn, DOC_3)
    finally:
        conn.close()

    app = create_app(db_path, storage_root, classifier=StubDocumentClassifier())
    with TestClient(app) as c:
        yield c, db_path


def test_select_category_over_failed_settles_chosen_category(client):
    # The manager picks `invoice` for a document whose machine outcome
    # FAILED (pdf_no_text, review required): the chosen category is
    # settled as the review's final category, recorded under the
    # verified manager (never the unverified header manager_id).
    c, db_path = client
    r = c.post(
        "/api/evidence/reviews",
        headers=HEADERS_T1_P1,
        json={
            "status": "select_category",
            "document_id": DOC_1,
            "property_id": PROP_1,
            "category": "invoice",
        },
    )

    assert r.status_code == 200
    body = r.json()
    # The chosen category is recorded under the verified manager.
    assert body["document_id"] == DOC_1
    assert body["final_category"] == "invoice"
    assert body["reviewed_by"] == MANAGER_9
    assert body["reviewed_by"] != TENANT_1
    assert body["reviewed_at"]  # generated by the backend, not the caller

    # Independent connection: the review record is REALLY persisted
    # (one row with action / final_category / reviewed_by / reviewed_at)
    # and the persisted reviewed_at is the same moment returned.
    check = sqlite3.connect(db_path)
    try:
        rev = check.execute(
            "SELECT review_id, action, final_category, reviewed_by, "
            "reviewed_at FROM evidence_review WHERE document_id = ?",
            (DOC_1,),
        ).fetchall()
        assert len(rev) == 1
        assert rev[0][1] == "select_category"
        assert rev[0][2] == "invoice"
        assert rev[0][3] == MANAGER_9
        assert datetime.fromisoformat(rev[0][4]) == datetime.fromisoformat(body["reviewed_at"])

        # The machine's FAILED outcome columns are NOT overwritten with
        # the human's category — only the details review_required flag
        # flips.
        details = json.loads(check.execute(
            "SELECT details FROM evidence_classification WHERE document_id = ?",
            (DOC_1,),
        ).fetchone()[0])
        assert details["review_required"] is False
        assert details["code"] == "pdf_no_text"
        assert details["reason"] == "pages exist but no extractable text"

        row = check.execute(
            "SELECT status, classification_type, confidence, source, "
            "created_at, classified_at FROM evidence_classification "
            "WHERE document_id = ?",
            (DOC_1,),
        ).fetchone()
        assert row == ("failed", None, None, "stub", NOW, NOW)
        assert row[1] != "invoice"  # machine category untouched
    finally:
        check.close()


def test_select_category_twice_keeps_history_and_latest_wins(client):
    # The manager re-reviews the SAME document: first `invoice`, then
    # `inspection_record`. Both reviews are history rows in
    # `evidence_review`; list/get surface the LATEST effective review
    # (inspection_record) with the second response's reviewed_by /
    # reviewed_at, the review flag is cleared, the document appears
    # exactly once, and the machine's own FAILED outcome is never
    # overwritten.
    c, db_path = client
    r1 = c.post(
        "/api/evidence/reviews",
        headers=HEADERS_T1_P1,
        json={
            "status": "select_category",
            "document_id": DOC_1,
            "property_id": PROP_1,
            "category": "invoice",
        },
    )
    assert r1.status_code == 200
    body1 = r1.json()
    assert body1["final_category"] == "invoice"
    assert body1["reviewed_by"] == MANAGER_9

    r2 = c.post(
        "/api/evidence/reviews",
        headers=HEADERS_T1_P1,
        json={
            "status": "select_category",
            "document_id": DOC_1,
            "property_id": PROP_1,
            "category": "inspection_record",
        },
    )
    assert r2.status_code == 200
    body2 = r2.json()
    assert body2["document_id"] == DOC_1
    assert body2["final_category"] == "inspection_record"
    assert body2["reviewed_by"] == MANAGER_9
    assert body2["reviewed_at"]  # generated by the backend, not the caller

    # list: the document appears EXACTLY once, carrying the LATEST
    # review's final_category / reviewed_by / reviewed_at and the
    # cleared review flag.
    r = c.get(
        "/api/evidence/documents",
        params={"property_id": PROP_1},
        headers=TENANT_HEADERS,
    )
    assert r.status_code == 200
    refs = [ref for ref in r.json() if ref["document_id"] == DOC_1]
    assert len(refs) == 1
    ref = refs[0]
    assert ref["final_category"] == "inspection_record"
    assert ref["reviewed_by"] == body2["reviewed_by"]
    assert ref["reviewed_at"] == body2["reviewed_at"]
    assert ref["review_required"] is False

    # get: the same latest-review values (and the machine status kept).
    r = c.get(
        f"/api/evidence/documents/{DOC_1}",
        params={"property_id": PROP_1},
        headers=TENANT_HEADERS,
    )
    assert r.status_code == 200
    doc = r.json()
    assert doc["final_category"] == "inspection_record"
    assert doc["reviewed_by"] == body2["reviewed_by"]
    assert doc["reviewed_at"] == body2["reviewed_at"]
    assert doc["review_required"] is False
    assert doc["classification"] == "failed"  # machine status preserved

    # Independent connection: BOTH review rows are kept (history,
    # oldest first); the latest one (inspection_record) is the same
    # record the second response returned, and the machine's FAILED
    # outcome columns are NOT overwritten — only the details
    # review_required flag flips.
    check = sqlite3.connect(db_path)
    try:
        rows = check.execute(
            "SELECT action, final_category, reviewed_by, reviewed_at "
            "FROM evidence_review WHERE document_id = ? ORDER BY rowid",
            (DOC_1,),
        ).fetchall()
        assert len(rows) == 2
        assert rows[0][0] == "select_category"
        assert rows[0][1] == "invoice"
        assert rows[0][2] == MANAGER_9
        assert rows[1][0] == "select_category"
        assert rows[1][1] == "inspection_record"
        assert rows[1][2] == MANAGER_9
        assert datetime.fromisoformat(rows[1][3]) == datetime.fromisoformat(body2["reviewed_at"])

        row = check.execute(
            "SELECT status, classification_type, confidence, source, "
            "created_at, classified_at FROM evidence_classification "
            "WHERE document_id = ?",
            (DOC_1,),
        ).fetchone()
        assert row == ("failed", None, None, "stub", NOW, NOW)
        assert row[1] != "inspection_record"  # machine category untouched

        details = json.loads(check.execute(
            "SELECT details FROM evidence_classification WHERE document_id = ?",
            (DOC_1,),
        ).fetchone()[0])
        assert details["review_required"] is False
        assert details["code"] == "pdf_no_text"
        assert details["reason"] == "pages exist but no extractable text"
    finally:
        check.close()


def test_confirm_suggested_settles_machine_category(client):
    # The machine settled a valid suggestion (classified /
    # lease_document, review required): confirm_suggested settles the
    # machine's OWN category, recorded under the verified manager.
    c, db_path = client
    r = c.post(
        "/api/evidence/reviews",
        headers=HEADERS_T1_P1,
        json={
            "status": "confirm_suggested",
            "document_id": DOC_2,
            "property_id": PROP_1,
        },
    )

    assert r.status_code == 200
    body = r.json()
    # The machine's suggested category is recorded under the manager.
    assert body["document_id"] == DOC_2
    assert body["final_category"] == MACHINE_CATEGORY
    assert body["reviewed_by"] == MANAGER_9
    assert body["reviewed_by"] != TENANT_1
    assert body["reviewed_at"]  # generated by the backend, not the caller

    # Independent connection: the review record is REALLY persisted
    # (one row with action / final_category / reviewed_by / reviewed_at)
    # and the persisted reviewed_at is the same moment returned.
    check = sqlite3.connect(db_path)
    try:
        rev = check.execute(
            "SELECT review_id, action, final_category, reviewed_by, "
            "reviewed_at FROM evidence_review WHERE document_id = ?",
            (DOC_2,),
        ).fetchall()
        assert len(rev) == 1
        assert rev[0][1] == "confirm_suggested"
        assert rev[0][2] == MACHINE_CATEGORY
        assert rev[0][3] == MANAGER_9
        assert datetime.fromisoformat(rev[0][4]) == datetime.fromisoformat(body["reviewed_at"])

        # The details flag was cleared; the machine's own outcome
        # columns are untouched by the review.
        details = json.loads(check.execute(
            "SELECT details FROM evidence_classification WHERE document_id = ?",
            (DOC_2,),
        ).fetchone()[0])
        assert details["review_required"] is False
        assert details["rationale"] == "lease agreement text detected"

        row = check.execute(
            "SELECT status, classification_type, confidence, source, "
            "created_at, classified_at FROM evidence_classification "
            "WHERE document_id = ?",
            (DOC_2,),
        ).fetchone()
        assert row == ("classified", MACHINE_CATEGORY, 0.8, "stub", NOW, NOW)
    finally:
        check.close()


def test_confirm_suggested_over_failed_is_409_review_rejected(client):
    # The machine outcome FAILED (no confirmable suggestion):
    # confirm_suggested must be rejected with 409 + stable code
    # `review_rejected`, and the rejection must leave NOTHING behind:
    # no review row, the review_required flag still set.
    c, db_path = client
    r = c.post(
        "/api/evidence/reviews",
        headers=HEADERS_T1_P1,
        json={
            "status": "confirm_suggested",
            "document_id": DOC_3,
            "property_id": PROP_1,
        },
    )

    assert r.status_code == 409
    body = r.json()
    detail = body["detail"]
    assert detail["code"] == "review_rejected"
    # Client-safe message: no stack traces, no SQL, no internal
    # identifiers leak in the response.
    text = json.dumps(body).lower()
    assert "traceback" not in text
    assert "select" not in text
    assert "sqlite" not in text
    assert ".py" not in text
    assert "no confirmable" not in text  # no internal machine-status detail

    # Independent connection: NO review row was written, and the
    # pending-review state (failed outcome + review_required=True)
    # is byte-for-byte unchanged.
    check = sqlite3.connect(db_path)
    try:
        assert check.execute(
            "SELECT COUNT(*) FROM evidence_review WHERE document_id = ?",
            (DOC_3,),
        ).fetchone()[0] == 0
        row = check.execute(
            "SELECT status, classification_type, confidence, details, "
            "source, created_at, classified_at FROM evidence_classification "
            "WHERE document_id = ?",
            (DOC_3,),
        ).fetchone()
        assert row == ("failed", None, None, FAILED_DETAILS_JSON, "stub", NOW, NOW)
        assert json.loads(row[3])["review_required"] is True
    finally:
        check.close()


# ---------------------------------------------------------------------------
# Failure paths: every one must leave the evidence_review table empty and
# the seeded classification rows byte-for-byte unchanged (verified through
# a separate sqlite3 connection against the database file itself).
# ---------------------------------------------------------------------------


def _assert_review_table_empty(check: sqlite3.Connection) -> None:
    """No review row may exist for ANY seeded document."""
    for doc in (DOC_1, DOC_2, DOC_3):
        assert (
            check.execute(
                "SELECT COUNT(*) FROM evidence_review WHERE document_id = ?",
                (doc,),
            ).fetchone()[0]
            == 0
        ), f"unexpected review row for {doc}"


def _assert_doc3_classification_unchanged(check: sqlite3.Connection) -> None:
    """DOC_3's FAILED machine outcome (review_required=True) must be
    byte-for-byte unchanged after a rejected request."""
    row = check.execute(
        "SELECT status, classification_type, confidence, details, "
        "source, created_at, classified_at FROM evidence_classification "
        "WHERE document_id = ?",
        (DOC_3,),
    ).fetchone()
    assert row == ("failed", None, None, FAILED_DETAILS_JSON, "stub", NOW, NOW)
    assert json.loads(row[3])["review_required"] is True


def test_review_property_mismatch_is_403_forbidden(client):
    # The command's property_id does not match the auth context's
    # property: 403 `forbidden` before anything is read or written.
    c, db_path = client
    r = c.post(
        "/api/evidence/reviews",
        headers=HEADERS_T1_P1,
        json={
            "status": "confirm_suggested",
            "document_id": DOC_1,
            "property_id": "P-mismatch",
        },
    )

    assert r.status_code == 403
    detail = r.json()["detail"]
    assert detail["code"] == "forbidden"
    assert detail["detail"]

    # Independent connection: no review row anywhere, seeded
    # classification untouched.
    check = sqlite3.connect(db_path)
    try:
        _assert_review_table_empty(check)
        _assert_doc3_classification_unchanged(check)
    finally:
        check.close()


def test_review_without_manager_binding_is_403_forbidden(client):
    # Tenant T2 / property P2 exist in the tenant/property tables but
    # have NO `manager_property` binding: 403 `forbidden`.
    c, db_path = client
    r = c.post(
        "/api/evidence/reviews",
        headers={"Authorization": "Bearer test-review-unbound"},
        json={
            "status": "confirm_suggested",
            "document_id": DOC_1,
            "property_id": PROP_2,
        },
    )

    assert r.status_code == 403
    detail = r.json()["detail"]
    assert detail["code"] == "forbidden"
    assert detail["detail"]

    # Independent connection: no review row anywhere, seeded
    # classification untouched (T2/P2 owns no documents at all).
    check = sqlite3.connect(db_path)
    try:
        _assert_review_table_empty(check)
        _assert_doc3_classification_unchanged(check)
    finally:
        check.close()


def test_review_missing_document_in_scope_is_404_not_found(client):
    # In-scope tenant/property (T1/P1, binding verified) but the
    # document_id was never seeded: 404 `not_found` — scope passes,
    # the document itself does not exist.
    c, db_path = client
    r = c.post(
        "/api/evidence/reviews",
        headers=HEADERS_T1_P1,
        json={
            "status": "select_category",
            "document_id": DOC_MISSING,
            "property_id": PROP_1,
            "category": "invoice",
        },
    )

    assert r.status_code == 404
    detail = r.json()["detail"]
    assert detail["code"] == "not_found"
    assert detail["detail"]

    # Independent connection: no review row anywhere (the missing
    # document has none by construction), seeded classification
    # untouched.
    check = sqlite3.connect(db_path)
    try:
        _assert_review_table_empty(check)
        _assert_doc3_classification_unchanged(check)
    finally:
        check.close()


def test_select_category_with_unknown_category_is_422(client):
    # `DocumentCategory` deliberately has NO `unknown` member: a
    # manager must settle one of the five business categories, not
    # rubber-stamp the machine's indeterminate outcome.
    c, db_path = client
    r = c.post(
        "/api/evidence/reviews",
        headers=HEADERS_T1_P1,
        json={
            "status": "select_category",
            "document_id": DOC_1,
            "property_id": PROP_1,
            "category": "unknown",
        },
    )

    assert r.status_code == 422
    # Request-model validation (FastAPI default shape).
    assert "detail" in r.json()

    # Independent connection: nothing written, seeded classification
    # untouched.
    check = sqlite3.connect(db_path)
    try:
        _assert_review_table_empty(check)
        _assert_doc3_classification_unchanged(check)
    finally:
        check.close()


def test_select_category_missing_category_is_422(client):
    # `category` is required for `select_category`: omitting it is a
    # request-model validation failure (422), not a scope failure.
    c, db_path = client
    r = c.post(
        "/api/evidence/reviews",
        headers=HEADERS_T1_P1,
        json={
            "status": "select_category",
            "document_id": DOC_1,
            "property_id": PROP_1,
        },
    )

    assert r.status_code == 422
    # Request-model validation (FastAPI default shape).
    assert "detail" in r.json()

    # Independent connection: nothing written, seeded classification
    # untouched.
    check = sqlite3.connect(db_path)
    try:
        _assert_review_table_empty(check)
        _assert_doc3_classification_unchanged(check)
    finally:
        check.close()


def test_review_with_extra_manager_id_field_is_422(client):
    # The command carries NO manager_id field (extra="forbid"): an
    # extra `manager_id` in the request body is rejected at
    # request-model validation — the reviewer always comes from the
    # auth context, never from the command.
    c, db_path = client
    r = c.post(
        "/api/evidence/reviews",
        headers=HEADERS_T1_P1,
        json={
            "status": "select_category",
            "document_id": DOC_1,
            "property_id": PROP_1,
            "category": "invoice",
            "manager_id": MANAGER_9,
        },
    )

    assert r.status_code == 422
    # Request-model validation (FastAPI default shape).
    assert "detail" in r.json()

    # Independent connection: nothing written, seeded classification
    # untouched.
    check = sqlite3.connect(db_path)
    try:
        _assert_review_table_empty(check)
        _assert_doc3_classification_unchanged(check)
    finally:
        check.close()


@pytest.mark.parametrize("headers, expected", [
    ({"Authorization": "Bearer unknown-test-token"}, 401),
    (TENANT_HEADERS, 403),
    ({**TENANT_HEADERS, "X-Prototype-Manager-Id": MANAGER_9,
      "X-Prototype-Tenant-Id": "manager-session", "X-Prototype-Property-Id": PROP_1}, 403),
])
def test_review_identity_cannot_be_spoofed(client, headers, expected):
    c, db_path = client
    response = c.post("/api/evidence/reviews", headers=headers, json={
        "status": "select_category", "document_id": DOC_3,
        "property_id": PROP_1, "category": "invoice",
    })
    assert response.status_code == expected
    with sqlite3.connect(db_path) as check:
        _assert_review_table_empty(check)
        _assert_doc3_classification_unchanged(check)


def test_manager_headers_cannot_replace_verified_reviewer(client):
    c, db_path = client
    response = c.post("/api/evidence/reviews", headers={
        **HEADERS_T1_P1, "X-Prototype-Manager-Id": "forged-manager",
        "X-Prototype-Tenant-Id": TENANT_2, "X-Prototype-Property-Id": PROP_2,
    }, json={"status": "select_category", "document_id": DOC_3,
             "property_id": PROP_1, "category": "invoice"})
    assert response.status_code == 200
    assert response.json()["reviewed_by"] == MANAGER_9
    with sqlite3.connect(db_path) as check:
        assert check.execute("SELECT reviewed_by FROM evidence_review").fetchone() == (MANAGER_9,)


@pytest.mark.parametrize("document_id", [DOC_1, DOC_MISSING])
def test_other_property_document_is_indistinguishable_from_missing(client, monkeypatch, document_id):
    c, db_path = client
    with sqlite3.connect(db_path) as check:
        check.execute("INSERT INTO manager_property VALUES (?, ?, ?)", (MANAGER_9, PROP_2, NOW))
    monkeypatch.setenv("RENTMATE_AUTH_TOKENS", json.dumps({
        "test-other-property": {"tenant_id": "manager-session", "property_id": PROP_2,
                                "manager_id": MANAGER_9},
    }))
    response = c.post("/api/evidence/reviews", headers={"Authorization": "Bearer test-other-property"},
                      json={"status": "select_category", "document_id": document_id,
                            "property_id": PROP_2, "category": "invoice"})
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "not_found"
    with sqlite3.connect(db_path) as check:
        _assert_review_table_empty(check)
        _assert_doc3_classification_unchanged(check)
