"""Evidence upload: validation policy + storage of the original file.

Scope of this increment (SCRUM-8, first increment):
- Validate allowed file type, non-empty content and size limit, with
  explicit, stable rejection reasons.
- Store the ORIGINAL bytes plus metadata (document id, original
  filename, file type, size, upload time, storage reference).

Limits (RECORDED VALUES — the project had no predefined limits, so this
increment adopts and documents them here):
- Allowed extensions (case-insensitive): pdf, jpg, jpeg, png, doc,
  docx, xls, xlsx, txt
- Maximum file size: 10 MiB (10 * 1024 * 1024 bytes)
- Empty files (0 bytes) are always rejected.
Change these constants only with team review (shared contract values).

Validation order (each check reports a distinct reason):
1. file type not in the allowlist  -> FILE_TYPE_NOT_ALLOWED
2. zero-byte content               -> EMPTY_FILE
3. content larger than the cap     -> FILE_TOO_LARGE

Security note: the stored filename is a module-generated reference
(`<document_id>.<ext>`); the user-supplied filename is metadata only and
is never used to build a path (no path traversal via `../`).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from contracts.evidence import UploadRejectionCode, UploadRejected

# -- Adopted limits (recorded; no repo-wide limit existed before this) ---
ALLOWED_EXTENSIONS: frozenset[str] = frozenset(
    {"pdf", "jpg", "jpeg", "png", "doc", "docx", "xls", "xlsx", "txt"}
)
MAX_FILE_BYTES: int = 10 * 1024 * 1024  # 10 MiB

_ALLOWED_EXTENSIONS_CSV = ", ".join(sorted(ALLOWED_EXTENSIONS))
_MAX_MB = MAX_FILE_BYTES // (1024 * 1024)


@dataclass(frozen=True)
class ValidationResult:
    """Outcome of validating one upload (pure decision, no I/O)."""

    ok: bool
    rejection: UploadRejected | None = None
    file_type: str | None = None  # normalised extension, set when ok


def _extension(filename: str) -> str | None:
    """Lower-cased extension without the dot; None when absent/invalid."""
    base = os.path.basename(filename or "").strip()
    if not base:
        return None
    stem, ext = os.path.splitext(base)
    ext = ext.lstrip(".").lower()
    if not ext or not re.fullmatch(r"[a-z0-9]+", ext):
        return None
    return ext


def _rejection(code: UploadRejectionCode, reason: str, retryable: bool) -> UploadRejected:
    return UploadRejected(code=code, reason=reason, retryable=retryable)


def validate_upload(filename: str, content: bytes) -> ValidationResult:
    """Validate one upload against the adopted limits.

    Deterministic and I/O-free so it is trivially testable; the storage
    step (save_document) runs only on `ok`.
    """
    ext = _extension(filename)
    if ext is None or ext not in ALLOWED_EXTENSIONS:
        return ValidationResult(
            ok=False,
            rejection=_rejection(
                UploadRejectionCode.FILE_TYPE_NOT_ALLOWED,
                f"File type {ext or '<none>'!r} is not allowed. "
                f"Allowed types: {_ALLOWED_EXTENSIONS_CSV}.",
                retryable=True,
            ),
        )

    size = len(content)
    if size == 0:
        return ValidationResult(
            ok=False,
            rejection=_rejection(
                UploadRejectionCode.EMPTY_FILE,
                "The uploaded file is empty (0 bytes).",
                retryable=True,
            ),
        )

    if size > MAX_FILE_BYTES:
        return ValidationResult(
            ok=False,
            rejection=_rejection(
                UploadRejectionCode.FILE_TOO_LARGE,
                f"File size {size} bytes exceeds the limit of "
                f"{MAX_FILE_BYTES} bytes ({_MAX_MB} MiB).",
                retryable=True,
            ),
        )

    return ValidationResult(ok=True, file_type=ext)


def storage_filename(document_id: str, file_type: str) -> str:
    """A safe, collision-free stored filename derived from the module's
    own ID + validated extension. User input never shapes the path."""
    return f"{document_id}.{file_type}"


def save_document(
    root_dir: str, document_id: str, file_type: str, content: bytes
) -> str:
    """Write the original bytes under `root_dir/evidence/<document_id>.<ext>`.

    Returns the relative storage reference (the `storage_ref` persisted
    with the document). Only called after validation passed.
    """
    target_dir = os.path.join(root_dir, "evidence")
    os.makedirs(target_dir, exist_ok=True)
    rel_ref = f"evidence/{storage_filename(document_id, file_type)}"
    abs_path = os.path.join(root_dir, rel_ref)
    with open(abs_path, "wb") as fh:
        fh.write(content)
    return rel_ref


def remove_document(root_dir: str, document_id: str, file_type: str) -> None:
    """Best-effort removal of a stored original (orphan cleanup).

    Called when a later step of the upload fails so no half-saved file
    is left behind. Never raises: a cleanup failure must not mask the
    original error.
    """
    try:
        os.remove(os.path.join(root_dir, "evidence", storage_filename(document_id, file_type)))
    except OSError:
        pass
