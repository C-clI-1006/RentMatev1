"""Evidence HTTP router (SCRUM-8, first increment).

Mounts the property-scoped upload / list / get / download endpoints on
top of `EvidenceService`.

IDENTITY — PROTOTYPE, TEST-ONLY, NOT REAL AUTH
----------------------------------------------
The repo has no real auth/session component. In this prototype the
bearer token is matched server-side against the JSON token map in the
``RENTMATE_AUTH_TOKENS`` env var (same pattern as the maintenance /
review routers) and the identity fields come ONLY from that map:

    tenant_id     (required in the map entry)
    property_id   (required in the map entry)
    manager_id    (optional in the map entry)

No client-supplied identity headers are read. This replaces the
identity resolution only; scope/role authorisation (permissions
service) is layered on top separately. Not real authentication.

Status mapping
--------------
- upload: 200 with `status=accepted` | `status=rejected` + stable code
  (rejection is an explicit outcome, mirroring maintenance intake).
- reads:  403 `forbidden` / 404 `not_found` (never reveals out-of-scope
  existence); 200 on success, download streams the original bytes.
- text:   403 `forbidden` / 404 `not_found` like the reads; 200 with
  `status=extracted` | `status=manual_review_required` — a non-extractable
  document is an explicit OUTCOME (stable code), never an HTTP error, and
  the stored original is never modified or deleted by extraction.
- review: 200 with the settled `EvidenceReviewResult`; scope failures map
  like the reads (403 `forbidden` / 404 `not_found`); an unconfirmable
  machine outcome is a 409 with stable code `review_rejected` (no internal
  details). Request-model validation failures stay 422 (FastAPI default).

  The review reviewer is the token-mapped `manager_id`, verified through
  manager_property;
  the command never carries a `manager_id` field.
"""

from __future__ import annotations

import hmac
import json
import os
from typing import Annotated

from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from contracts.evidence import (
    EvidenceReviewCommand,
    EvidenceReviewResult,
    EvidenceUploadCommand,
)
from llm.classify import IDocumentClassifier, build_document_classifier
from shared import AuthContext

from .repository import EvidenceReviewRejectedError
from .service import EvidenceScopeError, EvidenceService
from .upload import MAX_FILE_BYTES

router = APIRouter(prefix="/evidence", tags=["evidence"])
bearer = HTTPBearer()

# Content types for the allowlisted file types (see upload.ALLOWED_EXTENSIONS).
# An explicit map keeps downloads deterministic: on Windows,
# `mimetypes.guess_type` consults the OS registry, which may not resolve
# e.g. ".pdf".
_MEDIA_TYPES: dict[str, str] = {
    "pdf": "application/pdf",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "doc": "application/msword",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xls": "application/vnd.ms-excel",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "txt": "text/plain",
}

def get_evidence_service(request: Request) -> EvidenceService:
    """Wired in main.create_app (thin wiring; see main.py)."""
    return request.app.state.evidence_service


def get_auth(
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(bearer)],
) -> AuthContext:
    """Resolve the PROTOTYPE auth context from the bearer token.

    TEST-ONLY: the token is matched server-side against the JSON token
    map in ``RENTMATE_AUTH_TOKENS``; the identity fields come only from
    that map, never from the request. Same matching and error handling
    as the maintenance / review routers.
    """
    try:
        accounts = json.loads(os.getenv("RENTMATE_AUTH_TOKENS", "{}"))
        account = next(
            (
                value
                for token, value in accounts.items()
                if hmac.compare_digest(token, credentials.credentials)
            ),
            None,
        )
        if not account:
            raise ValueError()
        return AuthContext(
            tenant_id=account["tenant_id"],
            property_id=account["property_id"],
            manager_id=account.get("manager_id"),
        )
    except (ValueError, TypeError, KeyError, AttributeError):
        raise HTTPException(401, "Invalid or unconfigured prototype session")


def _scope_error(exc: EvidenceScopeError) -> HTTPException:
    status = 404 if exc.code == "not_found" else 403
    return HTTPException(
        status_code=status,
        detail={"code": exc.code, "detail": exc.detail},
    )


@router.post("/documents")
async def upload_document(
    file: Annotated[UploadFile, File()],
    property_id: Annotated[str, Form()],
    auth: Annotated[AuthContext, Depends(get_auth)],
    service: Annotated[EvidenceService, Depends(get_evidence_service)],
) -> dict:
    """Upload one original document (multipart/form-data).

    200 body: `{"status": "accepted", "document": {...}}` carrying the
    post-save classification state (UC6, fail-closed: `classified`
    — including an honest `unknown` — or `failed`; the upload stays
    accepted in both cases, saved != classified), or
    `{"status": "rejected", "code": ..., "reason": ..., "retryable": ...}`
    (nothing is stored on rejection).
    """
    # Bounded read: never buffer more than the cap + 1 byte.
    content = await file.read(MAX_FILE_BYTES + 1)
    cmd = EvidenceUploadCommand(
        property_id=property_id,
        original_filename=file.filename or "",
        content=content,
    )
    try:
        result = service.upload(cmd, auth)
    except EvidenceScopeError as exc:
        # Scope failure (property mismatch / unverified binding) is a
        # rejected request, not an accepted one: no file is touched.
        raise _scope_error(exc) from exc
    return result.model_dump(mode="json")


@router.get("/documents")
def list_documents(
    property_id: Annotated[str, Query()],
    auth: Annotated[AuthContext, Depends(get_auth)],
    service: Annotated[EvidenceService, Depends(get_evidence_service)],
) -> list[dict]:
    """Property-scoped list of stored documents (newest first)."""
    try:
        refs = service.list_documents(property_id, auth)
    except EvidenceScopeError as exc:
        raise _scope_error(exc) from exc
    return [ref.model_dump(mode="json") for ref in refs]


@router.get("/documents/{document_id}")
def get_document(
    document_id: str,
    property_id: Annotated[str, Query()],
    auth: Annotated[AuthContext, Depends(get_auth)],
    service: Annotated[EvidenceService, Depends(get_evidence_service)],
) -> dict:
    """Property-scoped single-document read (403 out of scope, 404 absent)."""
    try:
        doc = service.get_document(document_id, property_id, auth)
    except EvidenceScopeError as exc:
        raise _scope_error(exc) from exc
    return doc.model_dump(mode="json")


@router.get("/documents/{document_id}/download")
def download_document(
    document_id: str,
    property_id: Annotated[str, Query()],
    auth: Annotated[AuthContext, Depends(get_auth)],
    service: Annotated[EvidenceService, Depends(get_evidence_service)],
) -> Response:
    """Property-scoped original-file download (streams the stored bytes)."""
    try:
        payload = service.download(document_id, property_id, auth)
    except EvidenceScopeError as exc:
        raise _scope_error(exc) from exc
    media_type = _MEDIA_TYPES.get(payload.document.file_type, "application/octet-stream")
    # Serve under the module-generated name; the original filename is
    # display metadata only and never shapes the served path.
    filename = f"{payload.document.document_id}.{payload.document.file_type}"
    return Response(
        content=payload.content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/documents/{document_id}/text")
def extract_document_text(
    document_id: str,
    property_id: Annotated[str, Query()],
    auth: Annotated[AuthContext, Depends(get_auth)],
    service: Annotated[EvidenceService, Depends(get_evidence_service)],
) -> dict:
    """Property-scoped PDF text extraction (READ-ONLY).

    200 body: `{"status": "extracted", ...}` with the extracted text and
    page count, or `{"status": "manual_review_required", "code": ...,
    "reason": ..., "retryable": false}` for a scanned / corrupt /
    encrypted / non-PDF document. Non-extraction is an explicit
    outcome, never an HTTP error — the stored original is never
    modified or deleted, and classification/association stay untouched.
    The extracted text is document DATA, never instructions.
    """
    try:
        result = service.extract_text(document_id, property_id, auth)
    except EvidenceScopeError as exc:
        raise _scope_error(exc) from exc
    return result.model_dump(mode="json")


@router.post("/reviews", response_model=EvidenceReviewResult)
def review_document(
    cmd: Annotated[
        EvidenceReviewCommand,
        Body(description="Review command: `confirm_suggested` or "
        "`select_category` (discriminated by `status`)."),
    ],
    auth: Annotated[AuthContext, Depends(get_auth)],
    service: Annotated[EvidenceService, Depends(get_evidence_service)],
) -> dict:
    """Settle one manager review of a document's classification outcome.

    200 body: the settled `EvidenceReviewResult` (`reviewed_by` is the
    verified manager_id from the server-side Bearer identity, never taken
    from the command; the command has no `manager_id` field).

    Errors: scope failures map like the reads (403 `forbidden` / 404
    `not_found`); an unconfirmable machine outcome is 409 with the stable
    code `review_rejected` and a client-safe message (no internal
    details); request-model validation failures stay 422.
    """
    try:
        result = service.review(cmd, auth)
    except EvidenceScopeError as exc:
        raise _scope_error(exc) from exc
    except EvidenceReviewRejectedError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "review_rejected",
                "detail": "The document cannot be reviewed as requested.",
            },
        ) from exc
    return result.model_dump(mode="json")


def build_evidence_service(
    db_path: str,
    storage_root: str,
    classifier: IDocumentClassifier | None = None,
) -> EvidenceService:
    """Wiring helper used by main.create_app (kept out of main.py).

    `classifier` is optional: the default is the live adapter from
    `build_document_classifier()`. When the LLM env config
    (LLM_BASE_URL / LLM_API_KEY) is missing, the live adapter fails
    closed per call — `ClassifyFailure(code="configuration")`, which
    the service persists as a `failed` classification routed to manual
    review. It never silently falls back to the stub. Tests that want
    the labelled `StubDocumentClassifier` inject it explicitly via
    `classifier`.
    """
    from .repository import EvidenceRepository

    repo = EvidenceRepository(db_path, storage_root)
    return EvidenceService(repo, storage_root, classifier or build_document_classifier())
