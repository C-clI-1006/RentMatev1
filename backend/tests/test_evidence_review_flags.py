"""Persistence tests for the UC6 classification `review_required` flag.

Targeted coverage for the audit marker `_advance_classification` writes
into `evidence_classification.details`:
- confident success -> `classified` + `review_required: false` (the
  service flag merges LAST, so a result's own details cannot shadow it)
- honest `unknown`  -> `failed`, empty classification_type,
  `review_required: true` + `classification_uncertain`
- extraction failure (corrupt / encrypted PDF) and classifier failure
  -> `failed` + `review_required: true` with the ORIGINAL code/reason

Every assertion re-reads the TEMP SQLite database through a FRESH
sqlite3 connection (not the in-memory objects returned by the upload),
and confirms the upload machine and the stored original bytes are
untouched in every case.

Determinism comes from an injected controlled classifier (a plain
`IDocumentClassifier` double whose next outcome the test sets
explicitly) — no real model, no network, no env config.
"""

from __future__ import annotations

import io
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import pytest
from pypdf import PdfReader, PdfWriter

from contracts.evidence import EvidenceUploadCommand
from evidence.router import build_evidence_service
from llm.classify import ClassifyFailure, ClassificationResult
from shared import AuthContext

TENANT_1 = "T1"
PROP_1 = "P1"

AUTH = AuthContext(tenant_id=TENANT_1, property_id=PROP_1)


def _seed_bindings(conn: sqlite3.Connection) -> None:
    now = "2026-10-22T00:00:00+00:00"
    conn.executemany(
        "INSERT INTO tenant (tenant_id, name, contact, created_at) VALUES (?, ?, ?, ?)",
        [(TENANT_1, "Tenant One", "c1", now)],
    )
    conn.executemany(
        "INSERT INTO property (property_id, name, address, created_at) VALUES (?, ?, ?, ?)",
        [(PROP_1, "Property One", "addr1", now)],
    )
    conn.executemany(
        "INSERT INTO tenant_property (tenant_id, property_id, role, created_at) "
        "VALUES (?, ?, 'manager', ?)",
        [(TENANT_1, PROP_1, now)],
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Hand-built PDF fixtures (same approach as test_evidence_extract.py)
# ---------------------------------------------------------------------------


def _write_pdf(objects: list[bytes]) -> bytes:
    """Emit a minimal PDF with a correct xref table for `objects`."""
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(f"{i} 0 obj\n".encode() + body + b"\nendobj\n")
    xref_pos = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n".encode())
    out.write(b"0000000000 65535 f \n")
    for off in offsets:
        out.write(f"{off:010d} 00000 n \n".encode())
    out.write(
        b"trailer\n<< /Size "
        + str(len(objects) + 1).encode()
        + b" /Root 1 0 R >>\nstartxref\n"
        + str(xref_pos).encode()
        + b"\n%%EOF"
    )
    return out.getvalue()


def build_text_pdf(page_texts: list[str]) -> bytes:
    """Build a valid text PDF with one text line per page."""
    n = len(page_texts)
    font = 3 + 2 * n
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        (
            "<< /Type /Pages /Kids ["
            + " ".join(f"{3 + 2 * k} 0 R" for k in range(n))
            + f"] /Count {n} >>"
        ).encode(),
    ]
    for k, text in enumerate(page_texts):
        content = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("latin-1")
        objects.append(
            (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                f"/Contents {4 + 2 * k} 0 R /Resources << /Font << /F1 {font} 0 R >> >> >>"
            ).encode()
        )
        objects.append(
            b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream"
        )
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    return _write_pdf(objects)


def build_encrypted_pdf(page_texts: list[str]) -> bytes:
    """Encrypt a hand-built text PDF (pypdf writer, user=owner password)."""
    writer = PdfWriter()
    writer.append(PdfReader(io.BytesIO(build_text_pdf(page_texts))))
    writer.encrypt(user_password="pw123", owner_password="pw123")
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Controlled classifier (labelled test double: no model, no network)
# ---------------------------------------------------------------------------


@dataclass
class ControllableClassifier:
    """Plain `IDocumentClassifier` double: the test sets its next outcome
    explicitly, and it counts how often the service asked it (extraction
    failures must never reach it)."""

    outcome: ClassificationResult | ClassifyFailure
    calls: int = 0

    source = "stub"

    def classify(
        self, *, text: str, filename: str, file_type: str
    ) -> ClassificationResult | ClassifyFailure:
        self.calls += 1
        assert text.strip()  # only ever invoked with extractable text
        return self.outcome


# ---------------------------------------------------------------------------
# Wiring & DB re-read helpers
# ---------------------------------------------------------------------------


def _build_service(tmp_path: Path, db_name: str, classifier: ControllableClassifier):
    """Temp DB + temp storage + service with the injected classifier."""
    db_path = str(tmp_path / db_name)
    storage_root = str(tmp_path / "storage")
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            (Path(__file__).resolve().parents[2] / "db" / "schema.sql").read_text(encoding="utf-8")
        )
        _seed_bindings(conn)
    finally:
        conn.close()

    service = build_evidence_service(db_path, storage_root, classifier)
    return service, db_path, storage_root


def _upload_pdf(service, content: bytes, filename: str = "doc.pdf"):
    result = service.upload(
        EvidenceUploadCommand(property_id=PROP_1, original_filename=filename, content=content),
        AUTH,
    )
    assert result.status == "accepted"
    doc = result.document
    assert doc.upload_status == "saved"
    return doc


def _classification_row(db_path: str, document_id: str):
    """Fresh connection -> a real re-read of the DB, not in-memory state."""
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            """
            SELECT status, classification_type, source, confidence, details
            FROM evidence_classification
            WHERE document_id = ?
            """,
            (document_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    return row[0], row[1], row[2], row[3], json.loads(row[4])


def _assert_upload_machine_and_original_untouched(db_path: str, storage_root: str, doc, content: bytes):
    """Classification must never touch the upload machine or the file."""
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT upload_status FROM evidence_document WHERE document_id = ?",
            (doc.document_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None and row[0] == "saved"
    assert (Path(storage_root) / doc.storage_ref).read_bytes() == content


# ---------------------------------------------------------------------------
# Success: confident category
# ---------------------------------------------------------------------------


def test_success_category_persists_review_required_false(tmp_path: Path):
    rationale = "Amount due and bank transfer phrasing."
    # The controlled result even carries its OWN `review_required` in its
    # details: the service-generated flag must merge LAST and win.
    classifier = ControllableClassifier(
        ClassificationResult(
            category="invoice",
            confidence=0.8,
            rationale=rationale,
            source="stub",
            details={"model": "none", "latency_ms": 12, "review_required": True},
        )
    )
    service, db_path, storage_root = _build_service(tmp_path, "success.db", classifier)
    content = build_text_pdf(["Invoice due: bank transfer received"])
    doc = _upload_pdf(service, content)

    assert classifier.calls == 1
    status, ctype, source, confidence, details = _classification_row(db_path, doc.document_id)
    assert status == "classified"
    assert ctype == "invoice"
    assert source == "stub"
    assert confidence == 0.8
    assert details["rationale"] == rationale
    assert details["model"] == "none"  # the result's own details survive
    assert details["latency_ms"] == 12
    assert details["review_required"] is False  # service flag won the merge

    _assert_upload_machine_and_original_untouched(db_path, storage_root, doc, content)


# ---------------------------------------------------------------------------
# Honest `unknown`: fail-closed, routed to manual review
# ---------------------------------------------------------------------------


def test_unknown_persists_failed_uncertain_and_review_required(tmp_path: Path):
    classifier = ControllableClassifier(
        ClassificationResult(
            category="unknown",
            confidence=0.5,
            rationale="No category matched the extracted text.",
            source="stub",
            details={"method": "controlled"},
        )
    )
    service, db_path, storage_root = _build_service(tmp_path, "unknown.db", classifier)
    content = build_text_pdf(["unrecognised gibberish text"])
    doc = _upload_pdf(service, content)

    assert classifier.calls == 1
    status, ctype, source, confidence, details = _classification_row(db_path, doc.document_id)
    assert status == "failed"
    assert ctype is None  # never a guessed (or `unknown`-as-label) category
    assert source == "stub"  # source / confidence of the honest result kept
    assert confidence == 0.5
    assert details["rationale"] == "No category matched the extracted text."
    assert details["method"] == "controlled"  # original details preserved
    assert details["code"] == "classification_uncertain"
    assert details["review_required"] is True

    _assert_upload_machine_and_original_untouched(db_path, storage_root, doc, content)


# ---------------------------------------------------------------------------
# Extraction failure: before the classifier, original reason kept
# ---------------------------------------------------------------------------


EXTRACTION_FAILURE_VARIANTS = [
    (
        "corrupt",
        b"this is definitely not a pdf at all",
        "pdf_corrupt",
        "could not be parsed as a usable PDF",
    ),
    (
        "encrypted",
        build_encrypted_pdf(["secret content"]),
        "pdf_encrypted",
        "The PDF is encrypted",
    ),
]


@pytest.mark.parametrize(
    "label, content, code, reason_fragment",
    EXTRACTION_FAILURE_VARIANTS,
    ids=[v[0] for v in EXTRACTION_FAILURE_VARIANTS],
)
def test_extraction_failure_persists_review_required_with_original_reason(
    tmp_path: Path, label, content, code, reason_fragment
):
    classifier = ControllableClassifier(
        ClassificationResult(
            category="lease_document",
            confidence=0.9,
            rationale="should never run",
            source="stub",
        )
    )
    service, db_path, storage_root = _build_service(
        tmp_path, f"extract_fail_{label}.db", classifier
    )
    doc = _upload_pdf(service, content, filename=f"{label}.pdf")

    # extraction fails before the classifier can run at all
    assert classifier.calls == 0
    status, ctype, source, confidence, details = _classification_row(db_path, doc.document_id)
    assert status == "failed"
    assert ctype is None
    assert source == "none"
    assert confidence is None
    assert details["code"] == code  # original extraction code kept
    assert reason_fragment in details["reason"]  # original reason kept
    assert details["review_required"] is True

    _assert_upload_machine_and_original_untouched(db_path, storage_root, doc, content)


# ---------------------------------------------------------------------------
# Classifier failure: original code + reason kept, review required
# ---------------------------------------------------------------------------


def test_classifier_failure_persists_review_required_with_original_reason(tmp_path: Path):
    original_detail = (
        "Live Qwen adapter is not configured (LLM_BASE_URL / "
        "LLM_API_KEY missing). Route to manual review."
    )
    classifier = ControllableClassifier(ClassifyFailure("configuration", original_detail))
    service, db_path, storage_root = _build_service(tmp_path, "classify_fail.db", classifier)
    content = build_text_pdf(["lease agreement with landlord and tenant"])
    doc = _upload_pdf(service, content)

    assert classifier.calls == 1
    status, ctype, source, confidence, details = _classification_row(db_path, doc.document_id)
    assert status == "failed"
    assert ctype is None  # never converted into a guessed category
    assert source == "stub"
    assert confidence is None
    assert details["code"] == "configuration"
    assert details["reason"] == original_detail  # original reason kept verbatim
    assert details["review_required"] is True

    _assert_upload_machine_and_original_untouched(db_path, storage_root, doc, content)
