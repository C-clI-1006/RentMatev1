"""Query tests for the review fields returned by list_documents / get_document.

Round scope: READ side only (no production-code changes). Verifies how the
repository's details -> review-fields mapping behaves through the real
HTTP endpoints (main.create_app + TestClient):

1. a document whose classification details carry
   `review_required: true/false` plus `code` / `reason` returns exactly
   those values (list AND get);
2. legacy rows (field missing, corrupted JSON, non-object JSON, wrong
   field types) yield `None` for the affected review fields — no error;
3. the HTTP responses never expose the whole persisted `details`
   (rationale / extra keys / internal test markers included), only the
   three review fields;
4. cross-property access is still rejected (403, no existence or data
   leak) and a property's list never contains another property's rows.

Determinism: uploads run through the app's DEFAULT classifier (the live
adapter, unconfigured in this offline run), so every such upload fails
closed to `code: "configuration"` + `review_required: true`. In that
failure case the stored details carry no model marker, so the
leak-detection tripwire is a dedicated `internal` marker planted into
the TEMP database after the DB re-read. Legacy rows are seeded directly
into the TEMP database (the same fixture approach as
test_evidence_upload.py) with explicit `created_at` timestamps.
"""

from __future__ import annotations

import io
import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from main import create_app
from llm.classify import StubDocumentClassifier
from test_evidence_review_flags import build_text_pdf

# Prototype identity (TEST-ONLY bearer tokens, not real authentication):
# the tenant_id / property_id come ONLY from the server-side
# RENTMATE_AUTH_TOKENS map, never from the request.
TENANT_1 = "T1"
PROP_1 = "P1"
TENANT_2 = "T2"
PROP_2 = "P2"

# Fictitious session tokens (TEST-ONLY, not real credentials).
TOKEN_T1_P1 = "ev-rfq-t1-p1-fixture-token"
# Same tenant T1 scoped to the OTHER property P2: an independent token whose
# scope matches its own (T1, P2) binding — never switched via request headers.
TOKEN_T1_P2 = "ev-rfq-t1-p2-fixture-token"
TOKEN_T2_P2 = "ev-rfq-t2-p2-fixture-token"


def _auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


HEADERS_T1_P1 = _auth_headers(TOKEN_T1_P1)
HEADERS_T1_P2 = _auth_headers(TOKEN_T1_P2)  # same tenant T1, other property P2
HEADERS_T2_P2 = _auth_headers(TOKEN_T2_P2)


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
                TOKEN_T1_P2: {"tenant_id": TENANT_1, "property_id": PROP_2},
                TOKEN_T2_P2: {"tenant_id": TENANT_2, "property_id": PROP_2},
            }
        ),
    )


# Internal markers that must NEVER reach an HTTP response: they live in
# the persisted `details` (a leaked `details` would drag these out).
INTERNAL_MARKER = "INTERNAL-DO-NOT-LEAK-42"
LEGACY_MARKER = "LEGACY-SECRET-NOTE-77"

# (document_id, classification_status, details_text) — the legacy rows.
# review_required is deliberately ABSENT from every legacy row except
# LEG-005: the field predates it, so "not recorded" must surface as None.
LEGACY_DOCS = [
    ("LEG-001", "classified", '{"code": "old_record"}'),
    ("LEG-002", "failed", '{"rationale": "legacy text", "model": "none"}'),
    ("LEG-003", "classified", '"just a json string, not an object"'),
    ("LEG-004", "failed", '{"review_required": "yes", "code": 7, "reason": true}'),
    (
        "LEG-005",
        "failed",
        '{"review_required": true, "code": "manual_review", '
        f'"reason": "needs a human, {LEGACY_MARKER}", "internal": "{INTERNAL_MARKER}"}}',
    ),
]

LEGACY_CREATED_AT = "2026-10-20T00:00:00+00:00"  # older than any live upload


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
    # T1 (the normal-read tenant) holds `resident` bindings on BOTH
    # properties: P1 for the normal scope, P2 for the "same tenant, other
    # property" case (its token is scoped to P2). T2 keeps a `resident`
    # binding on P2 only.
    conn.execute(
        "INSERT INTO tenant_property (tenant_id, property_id, role, created_at) "
        "VALUES (?, ?, 'resident', ?)",
        (TENANT_1, PROP_1, now),
    )
    conn.execute(
        "INSERT INTO tenant_property (tenant_id, property_id, role, created_at) "
        "VALUES (?, ?, 'resident', ?)",
        (TENANT_1, PROP_2, now),
    )
    conn.execute(
        "INSERT INTO tenant_property (tenant_id, property_id, role, created_at) "
        "VALUES (?, ?, 'resident', ?)",
        (TENANT_2, PROP_2, now),
    )
    conn.commit()


def _seed_legacy_documents(conn: sqlite3.Connection) -> None:
    """Legacy rows inserted straight into the TEMP DB: the service's
    repository connection is still the one wired into the app, so every
    HTTP read sees these rows (fresh connection here, like the other
    evidence tests' DB re-reads)."""
    for doc_id, status, details in LEGACY_DOCS:
        conn.execute(
            """
            INSERT INTO evidence_document (
                document_id, tenant_id, property_id, original_filename,
                file_type, size_bytes, storage_ref, upload_status,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'pdf', 1, 'evidence/legacy.pdf', 'saved', ?, ?)
            """,
            (doc_id, TENANT_1, PROP_1, f"{doc_id}.pdf", LEGACY_CREATED_AT, LEGACY_CREATED_AT),
        )
        conn.execute(
            """
            INSERT INTO evidence_classification (
                document_id, status, source, details, created_at
            ) VALUES (?, ?, 'none', ?, ?)
            """,
            (doc_id, status, details, LEGACY_CREATED_AT),
        )
    conn.commit()


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = str(tmp_path / "review_query.db")
    storage_root = str(tmp_path / "review_storage")

    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            (Path(__file__).resolve().parents[2] / "db" / "schema.sql").read_text(encoding="utf-8")
        )
        _seed_bindings(conn)
        _seed_legacy_documents(conn)
    finally:
        conn.close()

    # Map the fictitious tokens (server-side RENTMATE_AUTH_TOKENS) onto the
    # seeded tenants/properties for the duration of the fixture.
    _seed_auth_tokens(monkeypatch)

    app = create_app(db_path, storage_root, classifier=StubDocumentClassifier())
    with TestClient(app) as c:
        yield c


def _upload_pdf(client: TestClient, filename: str, content: bytes) -> dict:
    r = client.post(
        "/api/evidence/documents",
        headers=HEADERS_T1_P1,
        files={"file": (filename, io.BytesIO(content))},
        data={"property_id": PROP_1},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "accepted"
    return body["document"]


def _list(client: TestClient, property_id: str, headers: dict):
    return client.get(
        "/api/evidence/documents", params={"property_id": property_id}, headers=headers
    )


def _get(client: TestClient, document_id: str, property_id: str, headers: dict):
    return client.get(
        f"/api/evidence/documents/{document_id}",
        params={"property_id": property_id},
        headers=headers,
    )


# ---------------------------------------------------------------------------
# 1. Saved review fields come back correctly (list AND get)
# ---------------------------------------------------------------------------


def test_saved_review_fields_returned_in_list_and_get(client):
    # The controlled classifier is NOT injected here (default wiring),
    # so use a direct service-level document instead: seed a row whose
    # details carry review_required=true + code + reason, then read it
    # back over HTTP. LEG-005 already plays the `true` case; assert it
    # end-to-end, in BOTH endpoints.
    r = _list(client, PROP_1, HEADERS_T1_P1)
    assert r.status_code == 200
    refs = {ref["document_id"]: ref for ref in r.json()}
    ref = refs["LEG-005"]
    assert ref["review_required"] is True
    assert ref["review_code"] == "manual_review"
    assert ref["review_reason"] == f"needs a human, {LEGACY_MARKER}"

    r = _get(client, "LEG-005", PROP_1, HEADERS_T1_P1)
    assert r.status_code == 200
    doc = r.json()
    assert doc["review_required"] is True
    assert doc["review_code"] == "manual_review"
    assert doc["review_reason"] == f"needs a human, {LEGACY_MARKER}"
    assert doc["classification"] == "failed"  # the row's own status is kept


def test_review_required_false_from_saved_details(client):
    """A saved `review_required: false` must survive as False — not be
    collapsed into None (the field WAS recorded) or into a missing key."""
    conn = sqlite3.connect(_db_path(client))
    try:
        conn.execute(
            """
            INSERT INTO evidence_document (
                document_id, tenant_id, property_id, original_filename,
                file_type, size_bytes, storage_ref, upload_status,
                created_at, updated_at
            ) VALUES ('LEG-101', ?, ?, 'f.pdf', 'pdf', 1,
                        'evidence/legacy.pdf', 'saved', ?, ?)
            """,
            (TENANT_1, PROP_1, LEGACY_CREATED_AT, LEGACY_CREATED_AT),
        )
        conn.execute(
            """
            INSERT INTO evidence_classification (
                document_id, status, source, details, created_at
            ) VALUES ('LEG-101', 'classified', 'none',
                        '{"review_required": false, "code": "ok", "reason": "auto"}', ?)
            """,
            (LEGACY_CREATED_AT,),
        )
        conn.commit()
    finally:
        conn.close()

    r = _list(client, PROP_1, HEADERS_T1_P1)
    ref = next(ref for ref in r.json() if ref["document_id"] == "LEG-101")
    assert ref["review_required"] is False
    assert ref["review_code"] == "ok"
    assert ref["review_reason"] == "auto"

    r = _get(client, "LEG-101", PROP_1, HEADERS_T1_P1)
    doc = r.json()
    assert doc["review_required"] is False
    assert doc["review_code"] == "ok"
    assert doc["review_reason"] == "auto"


def _db_path(client: TestClient) -> str:
    """The temp DB the fixture wired into the app (single connection)."""
    return client.app.state.evidence_service._repo._db_path


# ---------------------------------------------------------------------------
# 2. Legacy rows: missing / corrupted / non-object / wrong type -> None
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "document_id, expected",
    [
        # field missing -> None (never a default False)
        ("LEG-001", (None, "old_record", None)),
        # no review field at all -> all None
        ("LEG-002", (None, None, None)),
        # corrupted / non-object JSON -> all None
        ("LEG-003", (None, None, None)),
        # wrong types (str / int / bool-as-reason) -> all None
        ("LEG-004", (None, None, None)),
    ],
    ids=["missing-field", "no-review-fields", "non-object-json", "wrong-types"],
)
def test_legacy_rows_return_none_without_errors(client, document_id, expected):
    required, code, reason = expected

    r = _list(client, PROP_1, HEADERS_T1_P1)
    assert r.status_code == 200
    ref = next(ref for ref in r.json() if ref["document_id"] == document_id)
    assert (ref["review_required"], ref["review_code"], ref["review_reason"]) == (
        required,
        code,
        reason,
    )

    r = _get(client, document_id, PROP_1, HEADERS_T1_P1)
    assert r.status_code == 200
    doc = r.json()
    assert (doc["review_required"], doc["review_code"], doc["review_reason"]) == (
        required,
        code,
        reason,
    )


def test_live_upload_details_are_not_leaked(client, monkeypatch):
    """A test classifier simulating an unconfigured adapter fails closed
    to `failed` + `review_required: True`
    with `code: "configuration"`. The HTTP responses may surface only
    the three review fields — never the whole persisted details: after
    the DB re-read we also plant a dedicated internal marker into the
    stored details (keeping the original fields) and verify neither the
    `internal` key nor the marker can leak through list/get."""
    from llm.classify import ClassifyFailure

    class ConfigurationFailureClassifier:
        source = "stub"

        def classify(self, **kwargs):
            return ClassifyFailure(code="configuration", detail="Test-only configuration failure")

    monkeypatch.setattr(client.app.state.evidence_service, "_classifier", ConfigurationFailureClassifier())
    content = build_text_pdf(["lease agreement between landlord and tenant"])
    doc = _upload_pdf(client, "live.pdf", content)
    assert doc["classification"] == "failed"  # unconfigured live adapter, offline
    assert doc["review_required"] is True

    # Re-read through a FRESH connection to see what was really stored.
    conn = sqlite3.connect(_db_path(client))
    try:
        details = json.loads(
            conn.execute(
                "SELECT details FROM evidence_classification WHERE document_id = ?",
                (doc["document_id"],),
            ).fetchone()[0]
        )
    finally:
        conn.close()
    assert details["code"] == "configuration"
    assert details["review_required"] is True

    # Plant a dedicated internal marker into the stored details (the
    # original fields are kept; a leaked `details` would drag it out).
    internal_marker = "LIVE-INTERNAL-DO-NOT-LEAK-99"
    details["internal"] = internal_marker
    conn = sqlite3.connect(_db_path(client))
    try:
        conn.execute(
            "UPDATE evidence_classification SET details = ? WHERE document_id = ?",
            (json.dumps(details), doc["document_id"]),
        )
        conn.commit()
    finally:
        conn.close()

    r = _list(client, PROP_1, HEADERS_T1_P1)
    assert r.status_code == 200
    body = r.text
    ref = next(ref for ref in r.json() if ref["document_id"] == doc["document_id"])
    # the persisted review fields come back correctly (list)
    assert ref["review_required"] is True
    assert ref["machine_category"] is None
    assert ref["review_code"] == "configuration"
    assert isinstance(ref["review_reason"], str) and ref["review_reason"]
    # only the three review fields are exposed, and the key is NOT `details`
    assert "details" not in ref and "classification_details" not in ref
    assert set(ref.keys()) == {
        "document_id",
        "original_filename",
        "file_type",
        "size_bytes",
        "upload_status",
        "classification",
        "machine_category",
        "association",
        "uploaded_at",
        "review_required",
        "review_code",
        "review_reason",
        "final_category",
        "reviewed_by",
        "reviewed_at",
    }
    # no human review record exists for this document -> all None (list)
    assert ref["final_category"] is None
    assert ref["reviewed_by"] is None
    assert ref["reviewed_at"] is None
    assert "internal" not in body
    assert internal_marker not in body  # the planted details key never leaks

    r = _get(client, doc["document_id"], PROP_1, HEADERS_T1_P1)
    assert r.status_code == 200
    doc_body = r.json()
    # the persisted review fields come back correctly (get)
    assert doc_body["review_required"] is True
    assert doc_body["review_code"] == "configuration"
    assert doc_body["review_reason"] == ref["review_reason"]
    # no human review record exists for this document -> all None (get)
    assert doc_body["final_category"] is None
    assert doc_body["reviewed_by"] is None
    assert doc_body["reviewed_at"] is None
    assert "details" not in doc_body and "classification_details" not in doc_body
    assert "internal" not in r.text
    assert internal_marker not in r.text  # the planted details key never leaks
    assert INTERNAL_MARKER not in r.text and LEGACY_MARKER not in r.text


def test_legacy_reason_surfaces_but_details_stays_hidden(client):
    """LEG-005's `reason` (which contains the legacy marker) is one of
    the three exposed fields, but the marker must not appear anywhere
    ELSE in the response (i.e. `internal` / other details keys are not
    exposed alongside it)."""
    r = _list(client, PROP_1, HEADERS_T1_P1)
    body = r.text
    assert INTERNAL_MARKER not in body  # the `internal` key is never exposed
    assert LEGACY_MARKER in body  # ...but the exposed `review_reason` is

    r = _get(client, "LEG-005", PROP_1, HEADERS_T1_P1)
    assert INTERNAL_MARKER not in r.text
    assert LEGACY_MARKER in r.text  # inside review_reason only


# ---------------------------------------------------------------------------
# 4. Cross-property access: rejected, no existence or data leak
# ---------------------------------------------------------------------------


def test_cross_property_list_get_forbidden_and_no_leak(client):
    content = build_text_pdf(["invoice due: bank transfer received"])
    doc = _upload_pdf(client, "p1doc.pdf", content)

    # T2 (bound to P2) requesting P1's list -> 403, no P1 rows in body
    r = _list(client, PROP_1, HEADERS_T2_P2)
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "forbidden"
    assert doc["document_id"] not in r.text
    assert INTERNAL_MARKER not in r.text and LEGACY_MARKER not in r.text

    # T2 requesting one P1 document id -> 403 (scope fails before lookup)
    r = _get(client, doc["document_id"], PROP_1, HEADERS_T2_P2)
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "forbidden"
    assert doc["document_id"] not in r.json()["detail"]["detail"]

    # T2's OWN list: 200 but empty — none of P1's rows may leak in
    r = _list(client, PROP_2, HEADERS_T2_P2)
    assert r.status_code == 200
    assert r.json() == []

    # T1's list is unaffected and still carries exactly its own six rows
    r = _list(client, PROP_1, HEADERS_T1_P1)
    assert r.status_code == 200
    ids = {ref["document_id"] for ref in r.json()}
    assert ids == {doc["document_id"]} | {d[0] for d in LEGACY_DOCS}
    assert all(
        "LEG-" not in ref["document_id"] for ref in _list(client, PROP_2, HEADERS_T2_P2).json()
    )


def test_same_tenant_other_property_cannot_read_p1(client):
    """Even a valid resident of tenant T1 whose token is scoped to the
    OTHER property (T1 on P2 asking for P1) is forbidden — the scope
    check is on the auth context, not just the tenant. The identity
    switch happens via a SEPARATE token scoped to (T1, P2), never via
    request headers."""
    doc = _upload_pdf(client, "another.pdf", build_text_pdf(["receipt total due"]))
    r = _get(client, doc["document_id"], PROP_1, HEADERS_T1_P2)
    assert r.status_code == 403
    # and T1 (scoped to P2) cannot discover P1 rows by listing its own property
    assert _list(client, PROP_2, HEADERS_T1_P2).json() == []
