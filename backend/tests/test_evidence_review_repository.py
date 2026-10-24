"""Repository tests for `EvidenceRepository.save_review` (manager review).

Run: pytest backend/tests/test_evidence_review_repository.py

Temp database (built from db/schema.sql + prototype seed rows) — no network,
no HTTP, no service layer. Every assertion about what was actually persisted
is re-read through a SEPARATE sqlite3 connection, so the tests verify the
database file itself, not the repository's in-memory view.

Coverage:
- normal save: details `review_required` -> false; the machine's own outcome
  (classification_type / source / confidence / created_at / classified_at)
  and the original review code/reason are preserved byte-for-byte
- duplicate (same action, final_category AND reviewed_by) -> same record
  returned, no new row
- different reviewer OR a later corrected category -> appended, full history
  retained, details updated
- identical `reviewed_at` timestamps -> latest row is deterministic
  (reviewed_at DESC, rowid DESC)
- out-of-scope (wrong tenant / wrong property), missing classification row,
  and unparseable `details` -> all rejected, nothing written
- a failure of the details-update step after the review insert -> the WHOLE
  transaction rolls back (no review row, details untouched)
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from evidence.repository import EvidenceRepository, EvidenceReviewRejectedError

_SCHEMA_FILE = Path(__file__).resolve().parents[2] / "db" / "schema.sql"

TENANT_1 = "T1"
PROP_1 = "P1"
PROP_2 = "P2"

NOW = "2026-10-23T00:00:00+00:00"
TIE_TS = "2026-10-23T00:00:00+00:00"

DOC_1 = "doc-ev-review-1"
DOC_2 = "doc-ev-review-2"
DOC_3 = "doc-ev-review-3"

DETAILS_1 = {
    "code": "pdf_no_text",
    "reason": "pages exist but no extractable text",
    "review_required": True,
}
DETAILS_1_JSON = json.dumps(DETAILS_1)


def _seed_bindings(conn: sqlite3.Connection) -> None:
    conn.executemany(
        "INSERT INTO tenant (tenant_id, name, contact, created_at) VALUES (?, ?, ?, ?)",
        [(TENANT_1, "Tenant One", "c1", NOW)],
    )
    conn.executemany(
        "INSERT INTO property (property_id, name, address, created_at) VALUES (?, ?, ?, ?)",
        [(PROP_1, "Property One", "addr1", NOW), (PROP_2, "Property Two", "addr2", NOW)],
    )
    conn.executemany(
        "INSERT INTO tenant_property (tenant_id, property_id, role, created_at) "
        "VALUES (?, ?, 'manager', ?)",
        [(TENANT_1, PROP_1, NOW), (TENANT_1, PROP_2, NOW)],
    )
    conn.commit()


def _seed_doc(
    conn: sqlite3.Connection,
    document_id: str,
    tenant_id: str,
    property_id: str,
    *,
    with_classification: bool = True,
    details_json: str = DETAILS_1_JSON,
) -> None:
    conn.execute(
        "INSERT INTO evidence_document (document_id, tenant_id, property_id, "
        "original_filename, file_type, size_bytes, storage_ref, upload_status, "
        "created_at, updated_at) VALUES (?, ?, ?, 'a.pdf', 'pdf', 10, 'evidence/x.pdf', "
        "'saved', ?, ?)",
        (document_id, tenant_id, property_id, NOW, NOW),
    )
    if with_classification:
        conn.execute(
            "INSERT INTO evidence_classification (document_id, status, source, "
            "classification_type, confidence, details, created_at, classified_at) "
            "VALUES (?, 'failed', 'stub', 'unknown', 0.4, ?, ?, ?)",
            (document_id, details_json, NOW, NOW),
        )
    conn.commit()


def _make_repo(tmp_path: Path, doc: str, tenant: str = TENANT_1, prop: str = PROP_1, **kw):
    db_path = str(tmp_path / f"review_{doc}.db")
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(_SCHEMA_FILE.read_text(encoding="utf-8"))
        _seed_bindings(conn)
        _seed_doc(conn, doc, tenant, prop, **kw)
    finally:
        conn.close()
    return EvidenceRepository(db_path, str(tmp_path / "storage"))


def _details_of(db_path: str, document_id: str) -> str:
    check = sqlite3.connect(db_path)
    try:
        return check.execute(
            "SELECT details FROM evidence_classification WHERE document_id = ?",
            (document_id,),
        ).fetchone()[0]
    finally:
        check.close()


class _FailingUpdateProxy:
    """Stands in for the repository's sqlite3 connection, failing ONLY the
    details UPDATE — the step that runs AFTER the review row insert — so a
    real mid-transaction failure can be observed without touching prod code
    (sqlite3.Connection attributes are read-only and cannot be monkeypatched
    directly)."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def execute(self, sql: str, params=()) -> sqlite3.Cursor:
        if "UPDATE evidence_classification" in sql:
            raise sqlite3.OperationalError("simulated update failure")
        return self._conn.execute(sql, params)

    def __enter__(self) -> "_FailingUpdateProxy":
        self._conn.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb):
        # Propagate the same behaviour as the real connection: the exception
        # (our simulated OperationalError) triggers the ROLLBACK.
        return self._conn.__exit__(exc_type, exc, tb)

    def __getattr__(self, name: str):
        # Any other connection attribute passes through untouched.
        return getattr(self._conn, name)


def test_normal_save_clears_review_flag_and_preserves_machine_outcome(tmp_path):
    repo = _make_repo(tmp_path, DOC_1)
    db_path = str(tmp_path / f"review_{DOC_1}.db")
    try:
        created, rec = repo.save_review(DOC_1, TENANT_1, PROP_1, "confirm_suggested",
                                         "lease_document", "mgr-1")
        assert created is True
        assert rec["review_id"]
        assert rec["action"] == "confirm_suggested"
        assert rec["final_category"] == "lease_document"
        assert rec["reviewed_by"] == "mgr-1"
        assert rec["reviewed_at"]  # generated by the backend, not the caller

        # Independent connection: the flag is cleared, the original reason is
        # kept, and the machine's own outcome columns are untouched.
        check = sqlite3.connect(db_path)
        try:
            details = json.loads(_details_of(db_path, DOC_1))
            assert details["review_required"] is False
            assert details["code"] == "pdf_no_text"
            assert details["reason"] == "pages exist but no extractable text"
            row = check.execute(
                "SELECT classification_type, source, confidence, created_at, "
                "classified_at, status FROM evidence_classification WHERE document_id = ?",
                (DOC_1,),
            ).fetchone()
            assert row == ("unknown", "stub", 0.4, NOW, NOW, "failed")
            rev = check.execute(
                "SELECT review_id, action, final_category, reviewed_by FROM evidence_review "
                "WHERE document_id = ?",
                (DOC_1,),
            ).fetchall()
            assert len(rev) == 1
            assert rev[0][0] == rec["review_id"]
        finally:
            check.close()
    finally:
        repo.close()


def test_duplicate_review_returns_existing_record(tmp_path):
    repo = _make_repo(tmp_path, DOC_1)
    db_path = str(tmp_path / f"review_{DOC_1}.db")
    try:
        created1, rec1 = repo.save_review(DOC_1, TENANT_1, PROP_1, "confirm_suggested",
                                          "lease_document", "mgr-1")
        created2, rec2 = repo.save_review(DOC_1, TENANT_1, PROP_1, "confirm_suggested",
                                          "lease_document", "mgr-1")
        assert created1 is True
        assert created2 is False
        assert rec2 == rec1  # the SAME record, not a fresh one

        check = sqlite3.connect(db_path)
        try:
            assert check.execute(
                "SELECT COUNT(*) FROM evidence_review WHERE document_id = ?", (DOC_1,)
            ).fetchone()[0] == 1
        finally:
            check.close()
        assert _details_of(db_path, DOC_1) == json.dumps(
            {**DETAILS_1, "review_required": False}
        )
    finally:
        repo.close()


def test_different_reviewer_or_category_appends_history(tmp_path):
    repo = _make_repo(tmp_path, DOC_3)
    db_path = str(tmp_path / f"review_{DOC_3}.db")
    try:
        created1, rec1 = repo.save_review(DOC_3, TENANT_1, PROP_1, "confirm_suggested",
                                          "invoice", "mgr-1")
        created2, rec2 = repo.save_review(DOC_3, TENANT_1, PROP_1, "confirm_suggested",
                                          "maintenance_photo", "mgr-2")
        assert created1 is True and created2 is True
        assert rec2["reviewed_by"] == "mgr-2"
        assert rec2["final_category"] == "maintenance_photo"

        # History is retained (2 rows), the LATEST row wins the effective
        # review, and the details flag follows the latest review.
        check = sqlite3.connect(db_path)
        try:
            rows = check.execute(
                "SELECT final_category, reviewed_by FROM evidence_review "
                "WHERE document_id = ? ORDER BY rowid",
                (DOC_3,),
            ).fetchall()
            assert rows == [("invoice", "mgr-1"), ("maintenance_photo", "mgr-2")]
            latest = check.execute(
                "SELECT final_category, reviewed_by FROM evidence_review "
                "WHERE document_id = ? ORDER BY reviewed_at DESC, rowid DESC LIMIT 1",
                (DOC_3,),
            ).fetchone()
            assert latest == ("maintenance_photo", "mgr-2")
        finally:
            check.close()
        details = json.loads(_details_of(db_path, DOC_3))
        assert details["review_required"] is False
        assert details["code"] == "pdf_no_text"
        assert details["reason"] == "pages exist but no extractable text"
    finally:
        repo.close()


def test_identical_timestamps_keep_deterministic_latest(tmp_path):
    repo = _make_repo(tmp_path, DOC_3)
    db_path = str(tmp_path / f"review_{DOC_3}.db")
    # Two existing rows share the SAME reviewed_at timestamp (a tie).
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO evidence_review (review_id, document_id, action, "
            "final_category, reviewed_by, reviewed_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("rv-tie-a", DOC_3, "select_category", "invoice", "mgr-1", TIE_TS),
        )
        conn.execute(
            "INSERT INTO evidence_review (review_id, document_id, action, "
            "final_category, reviewed_by, reviewed_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("rv-tie-b", DOC_3, "select_category", "lease_document", "mgr-2", TIE_TS),
        )
        conn.commit()
    finally:
        conn.close()

    # A third review (different action / category / reviewer) is appended.
    created, rec = repo.save_review(DOC_3, TENANT_1, PROP_1, "confirm_suggested",
                                    "invoice", "mgr-1")
    assert created is True  # differs from the LATEST row, not the first
    # The latest must be the just-appended row: with the timestamps tied,
    # rowid DESC is the stable tie-breaker, so the highest rowid wins.
    assert rec["review_id"] != "rv-tie-a"
    check = sqlite3.connect(db_path)
    try:
        latest = check.execute(
            "SELECT review_id, final_category FROM evidence_review "
            "WHERE document_id = ? ORDER BY reviewed_at DESC, rowid DESC LIMIT 1",
            (DOC_3,),
        ).fetchone()
        assert latest[0] == rec["review_id"]
        assert latest[1] == "invoice"
    finally:
        check.close()
        repo.close()


def test_out_of_scope_writes_nothing(tmp_path):
    repo = _make_repo(tmp_path, DOC_1)
    db_path = str(tmp_path / f"review_{DOC_1}.db")
    # A different document owned by the SAME tenant under a SECOND property
    # (a cross-property claim on DOC_1 must still be rejected).
    conn = sqlite3.connect(db_path)
    try:
        _seed_doc(conn, DOC_2, TENANT_1, PROP_2)
    finally:
        conn.close()
    try:
        # Wrong tenant for the document.
        with pytest.raises(EvidenceReviewRejectedError):
            repo.save_review(DOC_1, "T-EVIL", PROP_1, "confirm_suggested",
                             "lease_document", "mgr-1")
        # Wrong property for the document.
        with pytest.raises(EvidenceReviewRejectedError):
            repo.save_review(DOC_1, TENANT_1, PROP_2, "confirm_suggested",
                             "lease_document", "mgr-1")

        # Nothing written by either rejected attempt.
        check = sqlite3.connect(db_path)
        try:
            assert check.execute(
                "SELECT COUNT(*) FROM evidence_review WHERE document_id = ?", (DOC_1,)
            ).fetchone()[0] == 0
        finally:
            check.close()
        assert _details_of(db_path, DOC_1) == DETAILS_1_JSON
    finally:
        repo.close()


def test_missing_classification_row_rejected(tmp_path):
    repo = _make_repo(tmp_path, DOC_2, with_classification=False)
    db_path = str(tmp_path / f"review_{DOC_2}.db")
    try:
        with pytest.raises(EvidenceReviewRejectedError):
            repo.save_review(DOC_2, TENANT_1, PROP_1, "confirm_suggested",
                             "lease_document", "mgr-1")
        check = sqlite3.connect(db_path)
        try:
            assert check.execute(
                "SELECT COUNT(*) FROM evidence_review WHERE document_id = ?", (DOC_2,)
            ).fetchone()[0] == 0
            # The document's (missing) classification row was NOT created.
            assert check.execute(
                "SELECT COUNT(*) FROM evidence_classification WHERE document_id = ?",
                (DOC_2,),
            ).fetchone()[0] == 0
        finally:
            check.close()
    finally:
        repo.close()


def test_unparseable_details_rejected(tmp_path):
    repo = _make_repo(tmp_path, DOC_2, details_json="not-json")
    db_path = str(tmp_path / f"review_{DOC_2}.db")
    try:
        with pytest.raises(EvidenceReviewRejectedError):
            repo.save_review(DOC_2, TENANT_1, PROP_1, "confirm_suggested",
                             "lease_document", "mgr-1")
        # No review row, and the (corrupt) details were NOT overwritten with
        # a fresh empty object.
        check = sqlite3.connect(db_path)
        try:
            assert check.execute(
                "SELECT COUNT(*) FROM evidence_review WHERE document_id = ?", (DOC_2,)
            ).fetchone()[0] == 0
        finally:
            check.close()
        assert _details_of(db_path, DOC_2) == "not-json"
    finally:
        repo.close()


def test_update_failure_rolls_back_the_whole_transaction(tmp_path):
    repo = _make_repo(tmp_path, DOC_1)
    db_path = str(tmp_path / f"review_{DOC_1}.db")
    try:
        original_conn = repo._conn
        # sqlite3.Connection attributes are read-only, so fail the details
        # UPDATE (the step AFTER the review insert) through a thin proxy
        # that forwards everything else to the real connection.
        repo._conn = _FailingUpdateProxy(original_conn)
        try:
            with pytest.raises(sqlite3.OperationalError,
                               match="simulated update failure"):
                repo.save_review(DOC_1, TENANT_1, PROP_1, "confirm_suggested",
                                 "lease_document", "mgr-1")
        finally:
            repo._conn = original_conn

        # Nothing was written: no review row, details still the original
        # (the flag was NOT cleared, the reason was NOT lost).
        check = sqlite3.connect(db_path)
        try:
            assert check.execute(
                "SELECT COUNT(*) FROM evidence_review WHERE document_id = ?", (DOC_1,)
            ).fetchone()[0] == 0
        finally:
            check.close()
        assert _details_of(db_path, DOC_1) == DETAILS_1_JSON
    finally:
        repo.close()
