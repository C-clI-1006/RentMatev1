"""Real HTTP tests for the evidence text-extraction increment (SCRUM-8).

Runs against a TEMP database + TEMP storage directory built through
`main.create_app` — the same wiring a real deployment uses, with the
identity coming from the PROTOTYPE headers (TEST-ONLY: any client can
send any header; nothing here is real authentication).

The PDF fixtures are BUILT BY HAND (minimal but structurally valid
PDFs with a correct xref table, verified against pypdf) so the tests
need no binary assets. Only the encrypted fixture uses pypdf's
`PdfWriter.encrypt` on top of a hand-built page.

Coverage:
- normal extraction (text + page_count + ids + storage_ref, extraction
  advances nothing beyond the post-upload classification run, original
  bytes still round-trip through download)
- multi-page extraction (page count + page texts joined by newlines)
- blank-page PDF -> `manual_review_required` / `pdf_no_text`
- corrupt PDF (garbage / header-only / truncated) -> `pdf_corrupt`
- encrypted PDF -> `pdf_encrypted` (never decrypted)
- zero-page skeleton -> `pdf_corrupt` (unit-level on the pure extractor)
- non-PDF document -> `file_type_not_supported`
- failure leaves the stored original intact (disk bytes + download);
  classification reflects the post-upload run (explicit `failed` for
  unextractable PDFs, stub-labelled outcome otherwise), association
  stays `unassociated`
- cross-property access -> 403 (never reveals existence);
  unknown id inside own scope -> 404

The service advances classification synchronously on upload; these
tests pin a labelled `StubDocumentClassifier` by explicit injection
into `app.state.evidence_service` so the classification outcomes are
deterministic without any network or live model.

The extracted text is document DATA in every response: these tests
assert its content, and the module under test never interprets it.
"""

from __future__ import annotations

import io
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader, PdfWriter

from contracts.evidence import ManualReviewCode
from evidence.extract import extract_pdf_text
from llm.classify import StubDocumentClassifier
from main import create_app

# Prototype identity (TEST-ONLY headers, not real authentication)
TENANT_1 = "T1"
PROP_1 = "P1"
TENANT_2 = "T2"
PROP_2 = "P2"

HEADERS_T1_P1 = {
    "X-Prototype-Tenant-Id": TENANT_1,
    "X-Prototype-Property-Id": PROP_1,
}
HEADERS_T2_P2 = {
    "X-Prototype-Tenant-Id": TENANT_2,
    "X-Prototype-Property-Id": PROP_2,
}


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
    conn.executemany(
        "INSERT INTO tenant_property (tenant_id, property_id, role, created_at) VALUES (?, ?, 'manager', ?)",
        [(TENANT_1, PROP_1, now), (TENANT_2, PROP_2, now)],
    )
    conn.commit()


@pytest.fixture()
def client(tmp_path: Path):
    db_path = str(tmp_path / "evidence_extract.db")
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

    app = create_app(db_path, storage_root)
    # UC6: the service advances classification synchronously on upload.
    # These extraction tests are about the extraction path (READ-ONLY),
    # so they pin a DETERMINISTIC, LABELLED classifier (the stub) by
    # explicit injection — no network, no live model, no real config.
    # The classification/association assertions below then say exactly
    # what the stub produced.
    app.state.evidence_service._classifier = StubDocumentClassifier()
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# Hand-built PDF fixtures (no binary assets needed)
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
    """Build a valid text PDF with one text line per page.

    Layout: 1=catalog, 2=pages node, page k = 3+2k, content k = 4+2k
    (k zero-based), shared Helvetica font = 3+2*len(page_texts).
    """
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
        objects.append(b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream")
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    return _write_pdf(objects)


def build_zero_page_pdf() -> bytes:
    """A structurally valid PDF whose page tree has no pages."""
    return _write_pdf(
        [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [] /Count 0 >>",
        ]
    )


def build_encrypted_pdf(page_texts: list[str]) -> bytes:
    """Encrypt a hand-built text PDF (pypdf writer, user=owner password)."""
    writer = PdfWriter()
    writer.append(PdfReader(io.BytesIO(build_text_pdf(page_texts))))
    writer.encrypt(user_password="pw123", owner_password="pw123")
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def _upload(client: TestClient, filename: str, content: bytes, headers: dict, property_id: str):
    return client.post(
        "/api/evidence/documents",
        headers=headers,
        files={"file": (filename, io.BytesIO(content))},
        data={"property_id": property_id},
    )


def _upload_doc(client: TestClient, filename: str, content: bytes) -> dict:
    r = _upload(client, filename, content, HEADERS_T1_P1, PROP_1)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "accepted"
    return body["document"]


def _get_text(client: TestClient, doc_id: str, headers: dict, property_id: str):
    return client.get(
        f"/api/evidence/documents/{doc_id}/text",
        params={"property_id": property_id},
        headers=headers,
    )


def _download(client: TestClient, doc_id: str, headers: dict, property_id: str):
    return client.get(
        f"/api/evidence/documents/{doc_id}/download",
        params={"property_id": property_id},
        headers=headers,
    )


def _get_doc(client: TestClient, doc_id: str) -> dict:
    return client.get(
        f"/api/evidence/documents/{doc_id}",
        params={"property_id": PROP_1},
        headers=HEADERS_T1_P1,
    ).json()


# ---------------------------------------------------------------------------
# Normal path
# ---------------------------------------------------------------------------


def test_extract_text_success(client, tmp_path):
    content = build_text_pdf(["Hello RentMate evidence"])
    doc = _upload_doc(client, "lease.pdf", content)

    # State SAVED AT UPLOAD: the classification machine is advanced
    # exactly once, by the POST-UPLOAD run (already inside
    # `_upload_doc`). The injected stub matched "Hello RentMate
    # evidence" to no keyword and answered the honest `unknown`; an
    # honest `unknown` is never trusted as a classification hit, so
    # the upload persisted it as `failed` (routed to manual review
    # with `classification_uncertain`), never a guessed category. The
    # classification machine is independent: the upload itself stayed
    # `saved` and association was never touched. Pin this state
    # BEFORE extraction runs...
    post_upload = _get_doc(client, doc["document_id"])
    assert post_upload["upload_status"] == "saved"
    assert post_upload["classification"] == "failed"
    assert post_upload["association"] == "unassociated"

    r = _get_text(client, doc["document_id"], HEADERS_T1_P1, PROP_1)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "extracted"
    assert body["document_id"] == doc["document_id"]
    # the original file reference survives the round trip
    assert body["storage_ref"] == doc["storage_ref"]
    assert body["page_count"] == 1
    assert body["text"].strip() == "Hello RentMate evidence"

    # ...and prove extract_text is READ-ONLY afterwards: the state
    # after extraction is exactly the state the post-upload run
    # recorded — extraction advanced NOTHING on its own, and neither
    # classification nor association moved.
    g = _get_doc(client, doc["document_id"])
    assert g["classification"] == post_upload["classification"]
    assert g["association"] == post_upload["association"]

    # the stored original still round-trips byte-for-byte
    d = _download(client, doc["document_id"], HEADERS_T1_P1, PROP_1)
    assert d.status_code == 200
    assert d.content == content
    assert (tmp_path / "evidence_storage" / doc["storage_ref"]).read_bytes() == content


def test_extract_multi_page(client):
    content = build_text_pdf(["Page one", "Page two", "Page three"])
    doc = _upload_doc(client, "three-pages.pdf", content)

    body = _get_text(client, doc["document_id"], HEADERS_T1_P1, PROP_1).json()
    assert body["status"] == "extracted"
    assert body["page_count"] == 3
    assert body["text"].strip() == "Page one\nPage two\nPage three"


# ---------------------------------------------------------------------------
# Non-extractable documents: explicit outcomes, never HTTP errors
# ---------------------------------------------------------------------------


def test_blank_page_pdf_is_manual_no_text(client, tmp_path):
    content = build_text_pdf([""])
    doc = _upload_doc(client, "blank.pdf", content)

    body = _get_text(client, doc["document_id"], HEADERS_T1_P1, PROP_1).json()
    assert body["status"] == "manual_review_required"
    assert body["code"] == "pdf_no_text"
    assert body["retryable"] is False
    assert body["document_id"] == doc["document_id"]
    assert body["storage_ref"] == doc["storage_ref"]
    assert body["reason"]

    # failure must not delete or modify the original
    d = _download(client, doc["document_id"], HEADERS_T1_P1, PROP_1)
    assert d.status_code == 200
    assert d.content == content
    assert (tmp_path / "evidence_storage" / doc["storage_ref"]).read_bytes() == content


CORRUPT_VARIANTS = [
    ("garbage", b"this is definitely not a pdf at all"),
    ("header_only", b"%PDF-1.4\nnot really a pdf"),
    ("truncated", build_text_pdf(["x"])[:20]),
]


@pytest.mark.parametrize("label, content", CORRUPT_VARIANTS, ids=[v[0] for v in CORRUPT_VARIANTS])
def test_corrupt_pdf_is_manual_corrupt(client, tmp_path, label, content):
    doc = _upload_doc(client, f"corrupt-{label}.pdf", content)

    body = _get_text(client, doc["document_id"], HEADERS_T1_P1, PROP_1).json()
    assert body["status"] == "manual_review_required"
    assert body["code"] == "pdf_corrupt"
    assert body["retryable"] is False
    assert body["document_id"] == doc["document_id"]
    assert body["storage_ref"] == doc["storage_ref"]
    assert body["reason"]

    # the corrupt bytes are stored untouched and still downloadable
    d = _download(client, doc["document_id"], HEADERS_T1_P1, PROP_1)
    assert d.status_code == 200
    assert d.content == content
    assert (tmp_path / "evidence_storage" / doc["storage_ref"]).read_bytes() == content
    # ...and the classification machine shows the EXPLICIT failure:
    # the corrupt bytes could not be extracted, so the post-upload run
    # persisted `failed` (routed to manual review) — never a guessed
    # category. Association was never touched.
    g = _get_doc(client, doc["document_id"])
    assert g["classification"] == "failed"
    assert g["association"] == "unassociated"


def test_encrypted_pdf_is_manual_never_decrypted(client, tmp_path):
    content = build_encrypted_pdf(["secret content"])
    doc = _upload_doc(client, "locked.pdf", content)

    body = _get_text(client, doc["document_id"], HEADERS_T1_P1, PROP_1).json()
    assert body["status"] == "manual_review_required"
    assert body["code"] == "pdf_encrypted"
    assert body["retryable"] is False
    assert body["document_id"] == doc["document_id"]
    assert body["storage_ref"] == doc["storage_ref"]
    assert body["reason"]

    # still exactly the encrypted bytes on disk and in download
    d = _download(client, doc["document_id"], HEADERS_T1_P1, PROP_1)
    assert d.status_code == 200
    assert d.content == content
    assert (tmp_path / "evidence_storage" / doc["storage_ref"]).read_bytes() == content


def test_non_pdf_document_is_manual_file_type_not_supported(client, tmp_path):
    content = b"plain text note"
    doc = _upload_doc(client, "notes.txt", content)

    body = _get_text(client, doc["document_id"], HEADERS_T1_P1, PROP_1).json()
    assert body["status"] == "manual_review_required"
    assert body["code"] == "file_type_not_supported"
    assert body["retryable"] is False
    assert "txt" in body["reason"]
    assert body["document_id"] == doc["document_id"]
    assert body["storage_ref"] == doc["storage_ref"]

    d = _download(client, doc["document_id"], HEADERS_T1_P1, PROP_1)
    assert d.status_code == 200
    assert d.content == content


def test_zero_page_pdf_is_corrupt():
    """Unit-level: a pageless skeleton is corrupt, distinct from no-text."""
    outcome = extract_pdf_text(build_zero_page_pdf())
    assert not outcome.ok
    assert outcome.code is ManualReviewCode.PDF_CORRUPT


# ---------------------------------------------------------------------------
# Scope: extraction is property-scoped like every other evidence read
# ---------------------------------------------------------------------------


def test_extract_cross_property_forbidden(client):
    doc = _upload_doc(client, "lease.pdf", build_text_pdf(["Hello"]))

    r = _get_text(client, doc["document_id"], HEADERS_T2_P2, PROP_1)
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "forbidden"


def test_extract_property_param_mismatch_forbidden(client):
    doc = _upload_doc(client, "lease.pdf", build_text_pdf(["Hello"]))

    # headers say P1 but the query claims P2
    r = _get_text(client, doc["document_id"], HEADERS_T1_P1, PROP_2)
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "forbidden"


def test_extract_unknown_document_not_found(client):
    r = _get_text(client, "no-such-document", HEADERS_T1_P1, PROP_1)
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "not_found"
