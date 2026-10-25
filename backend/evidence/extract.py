"""Pure, byte-level PDF text extraction (SCRUM-8 extraction increment).

This module is deliberately PURE: it takes raw file bytes and returns a
structured outcome. No storage I/O, no database, no auth, no state —
the service layer supplies the bytes (via the repository's scoped
read) and the document metadata.

Rules (see contracts.evidence):
- A document that cannot be parsed as a usable PDF is `pdf_corrupt`.
  This includes a parseable-but-pageless skeleton: a PDF without a
  page tree is not usable, and is distinct from pages-present-but-
  textless (`pdf_no_text`).
- An encrypted PDF is `pdf_encrypted`. Extraction NEVER attempts to
  decrypt; the document goes to manual processing as-is.
- Pages exist but no extractable text (scanned / image-only) is
  `pdf_no_text` — emptiness is judged after stripping whitespace.
- Nothing here ever raises for document content: any parse/decode
  failure is reported as `pdf_corrupt`. The caller can therefore treat
  the outcome as a discriminated, explicit result, never an exception.
- The returned text is document DATA. It must never be interpreted as
  instructions by any consuming layer.
"""

from __future__ import annotations

import io
from dataclasses import dataclass

from pypdf import PdfReader

from contracts.evidence import ManualReviewCode


@dataclass(frozen=True)
class Extraction:
    """Outcome of parsing one PDF's bytes.

    `code is None` => success: `text` is the page texts joined with
    newlines and `page_count >= 1`. Otherwise `code` is the stable
    manual-review reason (`text` is "" for corrupt/encrypted and the
    whitespace-only joined text for no-text; `page_count` is 0 except
    for `pdf_no_text`, where it is the real page count).
    """

    code: ManualReviewCode | None
    text: str
    page_count: int

    @property
    def ok(self) -> bool:
        return self.code is None


def extract_pdf_text(data: bytes) -> Extraction:
    """Extract text from raw PDF bytes (see module docstring for rules)."""
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            # By rule: never attempt decryption — manual processing.
            return Extraction(ManualReviewCode.PDF_ENCRYPTED, "", 0)
        pages = list(reader.pages)
        if not pages:
            # pypdf parses skeletons leniently; a pageless "PDF" is
            # corrupt for our purposes (a real document has pages).
            return Extraction(ManualReviewCode.PDF_CORRUPT, "", 0)
        text = "\n".join(page.extract_text() or "" for page in pages)
    except Exception:  # noqa: BLE001 — extraction boundary: ANY parse/decode failure = pdf_corrupt
        return Extraction(ManualReviewCode.PDF_CORRUPT, "", 0)
    if not text.strip():
        return Extraction(ManualReviewCode.PDF_NO_TEXT, text, len(pages))
    return Extraction(None, text, len(pages))
