"""Service regressions using manager_property and temporary databases.

Retains persistence, machine outcome, rejection and history assertions.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from contracts.evidence import (
    DocumentCategory,
    ReviewConfirmSuggested,
    ReviewSelectCategory,
)
from evidence.repository import EvidenceRepository, EvidenceReviewRejectedError
from evidence.service import EvidenceScopeError, EvidenceService
from llm.classify import StubDocumentClassifier
from shared import AuthContext

_SCHEMA_FILE = Path(__file__).resolve().parents[2] / "db" / "schema.sql"

TENANT_1 = "T1"
PROP_1 = "P1"
MANAGER_9 = "mgr-9"  # verified through manager_property

NOW = "2026-10-23T00:00:00+00:00"

DOC_1 = "doc-ev-svc-review-1"
DOC_2 = "doc-ev-svc-select-1"
DOC_3 = "doc-ev-svc-scope-1"
DOC_4 = "doc-ev-svc-scope-4"  # in-scope seeded row, asserted unchanged
DOC_4_MISSING = "doc-ev-svc-scope-4-missing"  # never seeded
DOC_5 = "doc-ev-svc-dup-1"  # reviewed twice with the same command
DOC_6 = "doc-ev-svc-xten-1"  # reviewed by a manager of ANOTHER tenant

PROP_2 = "P2"  # a second property, used by the scope-rejection cases
TENANT_2 = "T2"  # a tenant WITHOUT a manager binding
# The manager's OWN tenant in the cross-tenant case: a different tenant
# from the document owner, with NO tenant_property binding of its own on
# PROP_1 (the manager never needs one).
TENANT_MGR = "T9"
MANAGER_1 = "M1"  # bound to PROP_1 via manager_property ONLY

# The machine's settled suggestion (status `classified`) that the
# manager is about to confirm.
MACHINE_CATEGORY = "lease_document"
DETAILS_1 = {
    "rationale": "lease agreement text detected",
    "review_required": True,
}
DETAILS_1_JSON = json.dumps(DETAILS_1)


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
    conn.commit()


def _seed_doc(
    conn: sqlite3.Connection,
    document_id: str,
    *,
    status: str = "classified",
    classification_type: str | None = MACHINE_CATEGORY,
    confidence: float | None = 0.8,
    details_json: str = DETAILS_1_JSON,
) -> None:
    conn.execute(
        "INSERT INTO evidence_document (document_id, tenant_id, property_id, "
        "original_filename, file_type, size_bytes, storage_ref, upload_status, "
        "created_at, updated_at) VALUES (?, ?, ?, 'lease.pdf', 'pdf', 10, "
        "'evidence/x.pdf', 'saved', ?, ?)",
        (document_id, TENANT_1, PROP_1, NOW, NOW),
    )
    conn.execute(
        "INSERT INTO evidence_classification (document_id, status, source, "
        "classification_type, confidence, details, created_at, classified_at) "
        "VALUES (?, ?, 'stub', ?, ?, ?, ?, ?)",
        (document_id, status, classification_type, confidence,
         details_json, NOW, NOW),
    )
    conn.commit()


def _make_service(
    tmp_path: Path, doc: str, **seed_doc_kwargs
) -> tuple[EvidenceService, EvidenceRepository]:
    db_path = str(tmp_path / f"svc_{doc}.db")
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(_SCHEMA_FILE.read_text(encoding="utf-8"))
        _seed_bindings(conn)
        _seed_doc(conn, doc, **seed_doc_kwargs)
    finally:
        conn.close()
    repo = EvidenceRepository(db_path, str(tmp_path / "storage"))
    # review() never classifies, but inject the labelled stub explicitly
    # so the default (live Qwen adapter) is not constructed at all.
    service = EvidenceService(repo, str(tmp_path / "storage"),
                              classifier=StubDocumentClassifier())
    return service, repo


# Machine outcomes that `confirm_suggested` must REJECT: an honest
# `unknown` suggestion (fail-closed, persisted as a `failed` row the
# service cannot confirm), a plain `failed` outcome, and an empty
# (NULL) category. Each keeps a distinct details payload so
# "unchanged" is checked per case.
REJECT_DOC = "doc-ev-svc-review-reject"
REJECT_CASES = [
    pytest.param(
        "classified", "unknown", 0.5,
        json.dumps({"rationale": "cannot tell", "review_required": True}),
        id="unknown",
    ),
    pytest.param(
        "failed", "lease_document", 0.8,
        json.dumps({"code": "pdf_no_text",
                    "reason": "pages exist but no extractable text",
                    "review_required": True}),
        id="failed",
    ),
    pytest.param(
        "classified", None, None,
        json.dumps({"rationale": "blank suggestion", "review_required": True}),
        id="empty_category",
    ),
]


def test_confirm_suggested_success_settles_machine_category(tmp_path):
    service, repo = _make_service(tmp_path, DOC_1)
    db_path = str(tmp_path / f"svc_{DOC_1}.db")
    try:
        # Manager identity is separately verified through manager_property.
        auth = AuthContext(tenant_id=TENANT_1, property_id=PROP_1,
                           manager_id=MANAGER_9)
        cmd = ReviewConfirmSuggested(document_id=DOC_1, property_id=PROP_1)

        result = service.review(cmd, auth)

        # Settled to the machine's own suggested category.
        assert result.document_id == DOC_1
        assert result.final_category is DocumentCategory.LEASE_DOCUMENT
        assert result.final_category.value == MACHINE_CATEGORY
        # Reviewer is the manager_id verified through manager_property.
        assert result.reviewed_by == MANAGER_9
        assert result.reviewed_by != TENANT_1
        assert result.reviewed_at  # generated by the backend, not the caller

        # Independent connection: the review record is REALLY persisted
        # (one row, with the action / category / reviewer / timestamp),
        # and the persisted reviewed_at is the same moment returned.
        check = sqlite3.connect(db_path)
        try:
            rev = check.execute(
                "SELECT review_id, action, final_category, reviewed_by, "
                "reviewed_at FROM evidence_review WHERE document_id = ?",
                (DOC_1,),
            ).fetchall()
            assert len(rev) == 1
            assert rev[0][1] == "confirm_suggested"
            assert rev[0][2] == MACHINE_CATEGORY
            assert rev[0][3] == MANAGER_9
            assert datetime.fromisoformat(rev[0][4]) == result.reviewed_at

            # The details flag was cleared, the rest of details kept.
            details = json.loads(check.execute(
                "SELECT details FROM evidence_classification WHERE document_id = ?",
                (DOC_1,),
            ).fetchone()[0])
            assert details["review_required"] is False
            assert details["rationale"] == "lease agreement text detected"

            # The machine's own outcome columns are untouched by the
            # review (it only settles the final category + the flag).
            row = check.execute(
                "SELECT classification_type, source, confidence, created_at, "
                "classified_at, status FROM evidence_classification "
                "WHERE document_id = ?",
                (DOC_1,),
            ).fetchone()
            assert row == (MACHINE_CATEGORY, "stub", 0.8, NOW, NOW, "classified")
        finally:
            check.close()
    finally:
        repo.close()


@pytest.mark.parametrize(
    ("status", "classification_type", "confidence", "details_json"),
    REJECT_CASES,
)
def test_confirm_suggested_rejects_unconfirmable_machine_outcome(
    tmp_path, status, classification_type, confidence, details_json
):
    service, repo = _make_service(
        tmp_path,
        REJECT_DOC,
        status=status,
        classification_type=classification_type,
        confidence=confidence,
        details_json=details_json,
    )
    db_path = str(tmp_path / f"svc_{REJECT_DOC}.db")
    try:
        auth = AuthContext(tenant_id=TENANT_1, property_id=PROP_1,
                           manager_id=MANAGER_9)
        cmd = ReviewConfirmSuggested(document_id=REJECT_DOC, property_id=PROP_1)

        with pytest.raises(EvidenceReviewRejectedError):
            service.review(cmd, auth)

        # Independent connection: nothing was written — no review row,
        # and the classification row (status / category / confidence /
        # details) is byte-for-byte the seeded value.
        check = sqlite3.connect(db_path)
        try:
            assert check.execute(
                "SELECT COUNT(*) FROM evidence_review WHERE document_id = ?",
                (REJECT_DOC,),
            ).fetchone()[0] == 0
            row = check.execute(
                "SELECT status, classification_type, confidence, details, "
                "source, created_at, classified_at FROM "
                "evidence_classification WHERE document_id = ?",
                (REJECT_DOC,),
            ).fetchone()
            assert row == (status, classification_type, confidence,
                           details_json, "stub", NOW, NOW)
        finally:
            check.close()
    finally:
        repo.close()


def test_select_category_over_failed_settles_chosen_category(tmp_path):
    # Machine failed (pdf_no_text, review_required): the manager CANNOT
    # confirm the machine's suggestion, but CAN pick a final category.
    # The chosen `invoice` is settled as the review's final category.
    service, repo = _make_service(
        tmp_path,
        DOC_2,
        status="failed",
        classification_type=None,
        confidence=None,
        details_json=json.dumps(
            {"code": "pdf_no_text",
             "reason": "pages exist but no extractable text",
             "review_required": True}
        ),
    )
    db_path = str(tmp_path / f"svc_{DOC_2}.db")
    try:
        auth = AuthContext(tenant_id=TENANT_1, property_id=PROP_1,
                           manager_id=MANAGER_9)
        cmd = ReviewSelectCategory(
            document_id=DOC_2,
            property_id=PROP_1,
            category=DocumentCategory.INVOICE,
        )

        result = service.review(cmd, auth)

        # The manager's chosen category, recorded under the tenant.
        assert result.document_id == DOC_2
        assert result.final_category is DocumentCategory.INVOICE
        assert result.final_category.value == "invoice"
        assert result.reviewed_by == MANAGER_9
        assert result.reviewed_by != TENANT_1
        assert result.reviewed_at  # generated by the backend, not the caller

        # Independent connection: the review record is persisted with the
        # human's choice, the flag is cleared, and the machine's own
        # FAILED outcome (status / NULL category / reason) is NOT written
        # over with the human's category.
        check = sqlite3.connect(db_path)
        try:
            rev = check.execute(
                "SELECT action, final_category, reviewed_by, reviewed_at "
                "FROM evidence_review WHERE document_id = ?",
                (DOC_2,),
            ).fetchall()
            assert len(rev) == 1
            assert rev[0][0] == "select_category"
            assert rev[0][1] == "invoice"
            assert rev[0][2] == MANAGER_9
            assert datetime.fromisoformat(rev[0][3]) == result.reviewed_at

            # The details flag is cleared; the machine's failure reason
            # (code / reason) is retained — only review_required flips.
            details = json.loads(check.execute(
                "SELECT details FROM evidence_classification WHERE document_id = ?",
                (DOC_2,),
            ).fetchone()[0])
            assert details["review_required"] is False
            assert details["code"] == "pdf_no_text"
            assert details["reason"] == "pages exist but no extractable text"

            # The machine's classification columns stay FAILED / NULL —
            # the human's `invoice` must never be written back to the
            # machine's own category column.
            row = check.execute(
                "SELECT status, classification_type, confidence, source, "
                "created_at, classified_at FROM evidence_classification "
                "WHERE document_id = ?",
                (DOC_2,),
            ).fetchone()
            assert row == ("failed", None, None, "stub", NOW, NOW)
            assert row[1] != "invoice"  # machine category untouched
        finally:
            check.close()
    finally:
        repo.close()


def test_select_category_by_manager_of_other_tenant(tmp_path):
    # Cross-tenant review: the uploaded document belongs to tenant T1 on
    # P1, but the manager's auth context carries a DIFFERENT tenant_id
    # (T9) with manager_id M1. M1 is bound to P1 through the verified
    # `manager_property` table ONLY — no `tenant_property` row is created
    # for M1/T9 (a manager's access never comes from a tenant binding).
    # The machine failed, so `select_category` is the only viable review.
    service, repo = _make_service(
        tmp_path,
        DOC_6,
        status="failed",
        classification_type=None,
        confidence=None,
        details_json=json.dumps(
            {"code": "pdf_no_text",
             "reason": "pages exist but no extractable text",
             "review_required": True}
        ),
    )
    db_path = str(tmp_path / f"svc_{DOC_6}.db")
    # Create the manager and its verified property assignment (existing
    # schema): manager(M1) + manager_property(M1, P1). No tenant_property
    # binding for M1 or for T9 on P1 — that would be faking a tenant
    # binding for the manager.
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO manager (manager_id, name, created_at) "
            "VALUES (?, ?, ?)",
            (MANAGER_1, "Manager One", NOW),
        )
        conn.execute(
            "INSERT INTO manager_property (manager_id, property_id, created_at) "
            "VALUES (?, ?, ?)",
            (MANAGER_1, PROP_1, NOW),
        )
        conn.execute(
            "INSERT INTO tenant (tenant_id, name, contact, created_at) "
            "VALUES (?, ?, ?, ?)",
            (TENANT_MGR, "Tenant Nine", "c9", NOW),
        )
        conn.commit()
    finally:
        conn.close()
    try:
        # The manager's auth context: tenant T9 (NOT the document owner),
        # property P1, manager_id M1. The router resolves this from the
        # server-side token map; the command never carries a manager_id.
        auth = AuthContext(tenant_id=TENANT_MGR, property_id=PROP_1,
                           manager_id=MANAGER_1)
        cmd = ReviewSelectCategory(
            document_id=DOC_6,
            property_id=PROP_1,
            category=DocumentCategory.INVOICE,
        )

        result = service.review(cmd, auth)

        # Success: the cross-tenant manager settles the chosen category,
        # recorded under the verified manager identity — never the auth
        # tenant (the document owner) and never any other tenant.
        assert result.document_id == DOC_6
        assert result.final_category is DocumentCategory.INVOICE
        assert result.final_category.value == "invoice"
        assert result.reviewed_by == MANAGER_1
        assert result.reviewed_by != TENANT_MGR
        assert result.reviewed_by != TENANT_1
        assert result.reviewed_at  # generated by the backend, not the caller

        # Independent connection: the review record is REALLY persisted
        # with the verified manager identity.
        check = sqlite3.connect(db_path)
        try:
            rev = check.execute(
                "SELECT review_id, action, final_category, reviewed_by, "
                "reviewed_at FROM evidence_review WHERE document_id = ?",
                (DOC_6,),
            ).fetchall()
            assert len(rev) == 1
            assert rev[0][1] == "select_category"
            assert rev[0][2] == "invoice"
            assert rev[0][3] == MANAGER_1
            assert datetime.fromisoformat(rev[0][4]) == result.reviewed_at

            # The document is STILL owned by the uploading tenant (T1) on
            # P1 — the cross-tenant review changed nothing about
            # ownership.
            owner = check.execute(
                "SELECT tenant_id, property_id FROM evidence_document "
                "WHERE document_id = ?",
                (DOC_6,),
            ).fetchone()
            assert owner == (TENANT_1, PROP_1)

            # The machine's own FAILED outcome is NOT overwritten by the
            # human's category: status / NULL category / failure reason
            # stay as seeded; only the details review_required flag
            # flipped (code / reason retained).
            row = check.execute(
                "SELECT status, classification_type, confidence, details, "
                "source, created_at, classified_at FROM "
                "evidence_classification WHERE document_id = ?",
                (DOC_6,),
            ).fetchone()
            assert row[0] == "failed"
            assert row[1] is None  # machine category untouched
            assert row[1] != "invoice"
            assert row[2] is None
            assert row[4:] == ("stub", NOW, NOW)
            details = json.loads(row[3])
            assert details["review_required"] is False
            assert details["code"] == "pdf_no_text"
            assert details["reason"] == "pages exist but no extractable text"

            # No tenant binding was faked for the manager: neither T9 nor
            # M1 appears in tenant_property for P1.
            assert check.execute(
                "SELECT COUNT(*) FROM tenant_property WHERE "
                "tenant_id IN (?, ?)",
                (TENANT_MGR, MANAGER_1),
            ).fetchone()[0] == 0
        finally:
            check.close()
    finally:
        repo.close()


def _assert_nothing_written(db_path: str, doc: str, expected_row: tuple) -> None:
    """Independent connection: no review row and the classification row
    is byte-for-byte the seeded one."""
    check = sqlite3.connect(db_path)
    try:
        assert check.execute(
            "SELECT COUNT(*) FROM evidence_review WHERE document_id = ?",
            (doc,),
        ).fetchone()[0] == 0
        row = check.execute(
            "SELECT status, classification_type, confidence, details, "
            "source, created_at, classified_at FROM evidence_classification "
            "WHERE document_id = ?",
            (doc,),
        ).fetchone()
        assert row == expected_row
    finally:
        check.close()


# Seeded machine outcome for the scope-rejection document: a
# confirmable `classified` row, so the scope check must reject BEFORE
# any review logic runs.
SCOPE_EXPECTED_ROW = (
    "classified", MACHINE_CATEGORY, 0.8, DETAILS_1_JSON, "stub", NOW, NOW
)

# The seeded DOC_4 row is a plain classified machine outcome (same
# shape as SCOPE_EXPECTED_ROW), asserted byte-for-byte unchanged.
SCOPE_EXISTING_DOC_ROW = SCOPE_EXPECTED_ROW


def test_review_request_property_mismatch_forbidden(tmp_path):
    # The document IS bound to PROP_1 and the auth context IS verified
    # for PROP_1 — but the command asks for PROP_2: forbidden, even
    # though the document could otherwise be confirmed.
    service, repo = _make_service(tmp_path, DOC_3)
    db_path = str(tmp_path / f"svc_{DOC_3}.db")
    try:
        auth = AuthContext(tenant_id=TENANT_1, property_id=PROP_1,
                           manager_id=MANAGER_9)
        cmd = ReviewConfirmSuggested(document_id=DOC_3, property_id=PROP_2)

        with pytest.raises(EvidenceScopeError) as excinfo:
            service.review(cmd, auth)
        assert excinfo.value.code == "forbidden"

        _assert_nothing_written(db_path, DOC_3, SCOPE_EXPECTED_ROW)
    finally:
        repo.close()


def test_review_tenant_without_manager_binding_forbidden(tmp_path):
    # Same tenant+property as the auth context, but this tenant has NO
    # `manager_property` binding: the scope check rejects even
    # though the document is a perfectly confirmable one.
    service, repo = _make_service(tmp_path, DOC_3)
    db_path = str(tmp_path / f"svc_{DOC_3}.db")
    # The unbound tenant exists but has no binding row (FK-safe).
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO tenant (tenant_id, name, contact, created_at) "
            "VALUES (?, ?, ?, ?)",
            (TENANT_2, "Tenant Two", "c2", NOW),
        )
        conn.commit()
    finally:
        conn.close()
    try:
        auth = AuthContext(tenant_id=TENANT_2, property_id=PROP_1,
                           manager_id="unbound-manager")
        cmd = ReviewConfirmSuggested(document_id=DOC_3, property_id=PROP_1)

        with pytest.raises(EvidenceScopeError) as excinfo:
            service.review(cmd, auth)
        assert excinfo.value.code == "forbidden"

        _assert_nothing_written(db_path, DOC_3, SCOPE_EXPECTED_ROW)
    finally:
        repo.close()


def test_review_request_property_mismatch_forbidden_doc_4(tmp_path):
    # Same mismatch as the DOC_3 case, on a fresh seeded document:
    # auth is verified for PROP_1, the command asks for PROP_2 —
    # forbidden, even though DOC_4 is a perfectly confirmable row.
    service, repo = _make_service(tmp_path, DOC_4)
    db_path = str(tmp_path / f"svc_{DOC_4}.db")
    try:
        auth = AuthContext(tenant_id=TENANT_1, property_id=PROP_1,
                           manager_id=MANAGER_9)
        cmd = ReviewConfirmSuggested(document_id=DOC_4, property_id=PROP_2)

        with pytest.raises(EvidenceScopeError) as excinfo:
            service.review(cmd, auth)
        assert excinfo.value.code == "forbidden"

        _assert_nothing_written(db_path, DOC_4, SCOPE_EXISTING_DOC_ROW)
    finally:
        repo.close()


def test_review_tenant_without_manager_binding_forbidden_doc_4(tmp_path):
    # Same unbound tenant as the DOC_3 case, on a fresh seeded
    # document: TENANT_2 has NO `manager_property` binding, so
    # the scope check rejects even though DOC_4 could otherwise be
    # confirmed.
    service, repo = _make_service(tmp_path, DOC_4)
    db_path = str(tmp_path / f"svc_{DOC_4}.db")
    # The unbound tenant exists but has no binding row (FK-safe).
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO tenant (tenant_id, name, contact, created_at) "
            "VALUES (?, ?, ?, ?)",
            (TENANT_2, "Tenant Two", "c2", NOW),
        )
        conn.commit()
    finally:
        conn.close()
    try:
        auth = AuthContext(tenant_id=TENANT_2, property_id=PROP_1,
                           manager_id="unbound-manager")
        cmd = ReviewConfirmSuggested(document_id=DOC_4, property_id=PROP_1)

        with pytest.raises(EvidenceScopeError) as excinfo:
            service.review(cmd, auth)
        assert excinfo.value.code == "forbidden"

        _assert_nothing_written(db_path, DOC_4, SCOPE_EXISTING_DOC_ROW)
    finally:
        repo.close()


def test_review_unknown_document_in_scope_not_found(tmp_path):
    # Tenant and property are BOTH in scope (verified manager binding),
    # but the requested document does not exist: not_found, before any
    # review logic runs.
    service, repo = _make_service(tmp_path, DOC_4)
    db_path = str(tmp_path / f"svc_{DOC_4}.db")
    try:
        auth = AuthContext(tenant_id=TENANT_1, property_id=PROP_1,
                           manager_id=MANAGER_9)
        cmd = ReviewConfirmSuggested(document_id=DOC_4_MISSING,
                                     property_id=PROP_1)

        with pytest.raises(EvidenceScopeError) as excinfo:
            service.review(cmd, auth)
        assert excinfo.value.code == "not_found"

        # Independent connection: the whole evidence_review table is
        # still empty (no review row for the missing doc OR for the
        # seeded DOC_4), and the seeded DOC_4 classification row is
        # byte-for-byte unchanged.
        check = sqlite3.connect(db_path)
        try:
            assert check.execute(
                "SELECT COUNT(*) FROM evidence_review"
            ).fetchone()[0] == 0
            row = check.execute(
                "SELECT status, classification_type, confidence, details, "
                "source, created_at, classified_at FROM "
                "evidence_classification WHERE document_id = ?",
                (DOC_4,),
            ).fetchone()
            assert row == SCOPE_EXISTING_DOC_ROW
        finally:
            check.close()
    finally:
        repo.close()


def test_duplicate_review_is_idempotent_single_record(tmp_path):
    # The same auth submits the SAME command for the SAME document
    # twice: the second call must return exactly the same settlement
    # (final category / reviewer / timestamp), persist NO second review
    # row, and leave the machine's own outcome untouched.
    service, repo = _make_service(tmp_path, DOC_5)
    db_path = str(tmp_path / f"svc_{DOC_5}.db")
    try:
        auth = AuthContext(tenant_id=TENANT_1, property_id=PROP_1,
                           manager_id=MANAGER_9)
        cmd = ReviewConfirmSuggested(document_id=DOC_5, property_id=PROP_1)

        first = service.review(cmd, auth)

        # Independent connection: capture the single review row's
        # review_id and the whole classification row BEFORE the second
        # call.
        check = sqlite3.connect(db_path)
        try:
            rev_before = check.execute(
                "SELECT review_id FROM evidence_review WHERE document_id = ?",
                (DOC_5,),
            ).fetchall()
            assert len(rev_before) == 1
            machine_before = check.execute(
                "SELECT status, classification_type, confidence, details, "
                "source, created_at, classified_at FROM "
                "evidence_classification WHERE document_id = ?",
                (DOC_5,),
            ).fetchone()
        finally:
            check.close()

        second = service.review(cmd, auth)

        # Both calls settle to the same machine category, under the
        # same tenant, at the same timestamp.
        assert first.document_id == DOC_5
        assert second.document_id == DOC_5
        assert first.final_category is DocumentCategory.LEASE_DOCUMENT
        assert second.final_category is first.final_category
        assert second.final_category.value == MACHINE_CATEGORY
        assert first.reviewed_by == MANAGER_9
        assert second.reviewed_by == first.reviewed_by
        assert second.reviewed_by != TENANT_1
        assert first.reviewed_at
        assert second.reviewed_at == first.reviewed_at

        # Independent connection: STILL exactly one review row, with the
        # SAME review_id the first call persisted.
        check = sqlite3.connect(db_path)
        try:
            rev_after = check.execute(
                "SELECT review_id, action, final_category, reviewed_by, "
                "reviewed_at FROM evidence_review WHERE document_id = ?",
                (DOC_5,),
            ).fetchall()
            assert len(rev_after) == 1
            assert rev_after[0][0] == rev_before[0][0]  # review_id unchanged
            assert rev_after[0][1] == "confirm_suggested"
            assert rev_after[0][2] == MACHINE_CATEGORY
            assert rev_after[0][3] == MANAGER_9
            assert datetime.fromisoformat(rev_after[0][4]) == first.reviewed_at

            # The machine's own outcome (and the flag already cleared by
            # the FIRST call) is byte-for-byte what it was before the
            # second call.
            machine_after = check.execute(
                "SELECT status, classification_type, confidence, details, "
                "source, created_at, classified_at FROM "
                "evidence_classification WHERE document_id = ?",
                (DOC_5,),
            ).fetchone()
            assert machine_after == machine_before
            # ...and exactly the expected post-review shape: the
            # machine's outcome columns are untouched, only the details
            # flag flipped by the first review.
            details_after = json.loads(machine_after[3])
            assert details_after == {
                "rationale": "lease agreement text detected",
                "review_required": False,
            }
            assert machine_after == ("classified", MACHINE_CATEGORY, 0.8,
                                     machine_after[3], "stub", NOW, NOW)
        finally:
            check.close()
    finally:
        repo.close()


def test_upload_category_visible_before_review_and_preserved_after_correction(tmp_path):
    from contracts.evidence import EvidenceUploadCommand
    service, repo = _make_service(tmp_path, "category-visibility")
    tenant = AuthContext(tenant_id=TENANT_1, property_id=PROP_1)
    manager = AuthContext(tenant_id=None, property_id=PROP_1, manager_id=MANAGER_9)
    sample = Path(__file__).resolve().parents[2] / "demo/samples/test-invoice.pdf"
    try:
        result = service.upload(EvidenceUploadCommand(property_id=PROP_1,
            original_filename="invoice.pdf", content=sample.read_bytes()), tenant)
        doc = result.document
        assert doc.machine_category == "invoice"
        assert doc.review_required is False
        assert doc.final_category is None and doc.reviewed_by is None
        assert service.get_document(doc.document_id, PROP_1, tenant).machine_category == "invoice"
        assert service.get_document(doc.document_id, PROP_1, manager).machine_category == "invoice"
        for auth in (tenant, manager):
            assert next(d for d in service.list_documents(PROP_1, auth)
                        if d.document_id == doc.document_id).machine_category == "invoice"
        service.review(ReviewSelectCategory(document_id=doc.document_id, property_id=PROP_1,
            category=DocumentCategory.LEASE_DOCUMENT), manager)
        reviewed = service.get_document(doc.document_id, PROP_1, manager)
        assert reviewed.machine_category == "invoice"
        assert reviewed.final_category == "lease_document"
        assert reviewed.reviewed_by == MANAGER_9
        with pytest.raises(EvidenceScopeError):
            service.get_document(doc.document_id, "another-property", tenant)
    finally:
        repo.close()


@pytest.mark.parametrize("status,category", [("failed", "invoice"), ("classified", "unknown")])
def test_unusable_machine_category_does_not_become_display_category(tmp_path, status, category):
    service, repo = _make_service(tmp_path, "unusable-category", status=status, classification_type=category)
    try:
        doc = service.get_document("unusable-category", PROP_1,
            AuthContext(tenant_id=TENANT_1, property_id=PROP_1))
        assert doc.machine_category is None
        assert doc.final_category is None
        assert doc.review_required is True
    finally:
        repo.close()
