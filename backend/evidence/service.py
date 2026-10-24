"""Evidence service: property-scope checks + upload orchestration.

This increment (SCRUM-8): upload with validation, storage, and
property-scoped query/download, plus UC6 document classification.
After a successful save, the injected `IDocumentClassifier` runs over
the extracted text (fail-closed): a result persists as `classified`
(including an honest `unknown`), a `ClassifyFailure` or extraction
failure persists as `failed` — the upload itself always stays
`SAVED`/`accepted`. Association stays `unassociated` (separate state
machines).

AUTH NOTE (important, by design): identity arrives only via
`shared.AuthContext` (tenant_id / property_id / manager_id), resolved
server-side by the router from the bearer-token mapping — never from
request bodies, query params or custom headers. Tenant operations
(upload / list / get / download / text) require a verified
`tenant_property` binding; manager review requires a verified
`manager_property` binding through the permissions service (default
deny, no tenant fallback). A denied check never degrades to a
tenant-scope success.

Failure cleanup: if the file write fails, any partial file is removed
and nothing reaches the DB; if the DB write fails (transaction rolled
back), the already-written file is removed. A failed upload leaves no
residue on disk or in the database.
"""

from __future__ import annotations

import json
from datetime import datetime

from shared import AuthContext, new_id, utcnow

from llm.classify import (
    ClassifyFailure,
    IDocumentClassifier,
    build_document_classifier,
)

from contracts.evidence import (
    ClassificationStatus,
    DocumentCategory,
    DownloadPayload,
    EvidenceDocument,
    EvidenceDocumentRef,
    EvidenceReviewCommand,
    EvidenceReviewResult,
    EvidenceUploadCommand,
    ExtractTextResult,
    ManualReviewCode,
    ReviewConfirmSuggested,
    ReviewSelectCategory,
    TextExtracted,
    TextManualReviewRequired,
    UploadAccepted,
    UploadResult,
    AssociationStatus,
    UploadStatus,
)

from permissions import ManagerAccessDeniedError, MissingManagerIdentityError

from .extract import extract_pdf_text
from .repository import EvidenceRepository, EvidenceReviewRejectedError
from .upload import remove_document, save_document, validate_upload


# Stable, human-readable reasons for the extractor's manual-review codes
# (the non-PDF case is built per-document in `extract_text`).
_MANUAL_REASONS: dict[ManualReviewCode, str] = {
    ManualReviewCode.PDF_CORRUPT: (
        "The stored file could not be parsed as a usable PDF. The original "
        "is unchanged and remains downloadable; process the document manually."
    ),
    ManualReviewCode.PDF_ENCRYPTED: (
        "The PDF is encrypted. Extraction never decrypts files; process the "
        "document manually."
    ),
    ManualReviewCode.PDF_NO_TEXT: (
        "The PDF has pages but no extractable text (e.g. a scanned or "
        "image-only document); transcribe the content manually."
    ),
}


class EvidenceScopeError(Exception):
    """Raised when a request cannot be served in scope.

    `code` is "forbidden" (scope/permission) or "not_found" (no such
    document within the verified scope). The router maps these to 403
    / 404 without revealing out-of-scope existence.
    """

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


class EvidenceService:
    """Property-scoped evidence operations for the evidence module."""

    def __init__(
        self,
        repo: EvidenceRepository,
        storage_root: str,
        classifier: IDocumentClassifier | None = None,
    ) -> None:
        self._repo = repo
        self._storage_root = storage_root
        # UC6: every module-level model call goes through the single llm
        # adapter. The default is the live Qwen adapter (build_document_classifier);
        # without LLM_BASE_URL / LLM_API_KEY it fails closed per call
        # (manual review), never guessing and never silently falling back
        # to a stub. Tests may inject a labelled stub explicitly.
        self._classifier = classifier if classifier is not None else build_document_classifier()

    # -- scope checks (every entry point) ------------------------------------

    def _check_scope(self, property_id: str, auth: AuthContext) -> None:
        """Verify the requested property is the auth context's property
        and that the TENANT's access to it is verified
        (`tenant_property` binding). Tenant upload / list / get /
        download / text use this check; no manager binding is
        required."""
        if property_id != auth.property_id:
            raise EvidenceScopeError(
                "forbidden",
                "The requested property is not the property bound to the "
                "auth context.",
            )
        if not self._repo.has_tenant_access(auth.tenant_id, auth.property_id):
            raise EvidenceScopeError(
                "forbidden",
                "This identity is not authorised for this property.",
            )

    def _check_manager_scope(self, property_id: str, auth: AuthContext) -> str:
        """Manager-only entry points: the server-side identity MUST carry
        a `manager_id` (a tenant identity is a denial, not a fallback)
        and the `manager_id` + `property_id` binding must be verified
        through the permissions service (default deny). Returns the
        verified `manager_id` for `reviewed_by`."""
        if property_id != auth.property_id:
            raise EvidenceScopeError(
                "forbidden",
                "The requested property is not the property bound to the "
                "auth context.",
            )
        try:
            self._repo.require_manager_access(auth.manager_id, auth.property_id)
        except (MissingManagerIdentityError, ManagerAccessDeniedError):
            raise EvidenceScopeError(
                "forbidden",
                "This identity is not authorised for this property.",
            ) from None
        return auth.manager_id

    # -- upload ----------------------------------------------------------------

    def upload(self, cmd: EvidenceUploadCommand, auth: AuthContext) -> UploadResult:
        self._check_scope(cmd.property_id, auth)

        validation = validate_upload(cmd.original_filename, cmd.content)
        if not validation.ok or validation.file_type is None:
            assert validation.rejection is not None
            return validation.rejection

        document_id = new_id()
        uploaded_at = utcnow()
        try:
            storage_ref = save_document(
                self._storage_root, document_id, validation.file_type, cmd.content
            )
        except OSError:
            # Filesystem failure: nothing is in the DB yet; remove any
            # partially written file so the failure leaves no residue.
            remove_document(self._storage_root, document_id, validation.file_type)
            raise

        doc = EvidenceDocument(
            document_id=document_id,
            tenant_id=auth.tenant_id,
            property_id=auth.property_id,
            original_filename=cmd.original_filename,
            file_type=validation.file_type,
            size_bytes=len(cmd.content),
            upload_status=UploadStatus.SAVED,
            classification=ClassificationStatus.PENDING,  # never fake success
            association=AssociationStatus.UNASSOCIATED,
            storage_ref=storage_ref,
            uploaded_at=uploaded_at,
            updated_at=uploaded_at,
        )
        try:
            self._repo.save_document(doc)
        except Exception:
            # DB write failed (its transaction rolled back): remove the
            # now-orphaned file so a failed upload leaves no residue.
            # (A failed save never reaches the classifier.)
            remove_document(self._storage_root, document_id, validation.file_type)
            raise

        # UC6: advance the classification machine ONLY after a
        # successful save. Fail-closed: any failure persists as
        # `failed` — never a guessed category; the upload itself stays
        # SAVED/accepted regardless of the classification outcome.
        self._advance_classification(doc, cmd.content, cmd.original_filename)
        return UploadAccepted(document=doc)

    def _advance_classification(
        self, doc: EvidenceDocument, content: bytes, filename: str
    ) -> None:
        """Run the UC6 classifier over the document and persist the
        outcome. The classification machine is separate from the upload
        machine: this never touches upload status or the stored file.

        - non-PDF: no text exists by construction -> `failed` with
          `file_type_not_supported` (routed to manual review).
        - corrupt / encrypted / no-text PDF: `failed` with the
          extraction code (manual review).
        - extractable text: the classifier runs; a confident,
          non-`unknown` result persists as `classified`, an honest
          `unknown` persists as `failed` with `classification_uncertain`
          (manual review), and a `ClassifyFailure` persists as `failed`
          (never a guess).
        """
        if doc.file_type != "pdf":
            self._persist_classification(
                doc,
                status="failed",
                classification_type=None,
                source="none",
                confidence=None,
                details={
                    "code": ManualReviewCode.FILE_TYPE_NOT_SUPPORTED.value,
                    "reason": "Text classification supports PDF documents only (UC6).",
                    "review_required": True,
                },
            )
            return

        outcome = extract_pdf_text(content)
        if not outcome.ok:
            assert outcome.code is not None
            self._persist_classification(
                doc,
                status="failed",
                classification_type=None,
                source="none",
                confidence=None,
                details={
                    "code": outcome.code.value,
                    "reason": _MANUAL_REASONS.get(outcome.code, "Manual processing required."),
                    "review_required": True,
                },
            )
            return

        result = self._classifier.classify(
            text=outcome.text, filename=filename, file_type=doc.file_type
        )
        if isinstance(result, ClassifyFailure):
            self._persist_classification(
                doc,
                status="failed",
                classification_type=None,
                source=self._classifier.source,
                confidence=None,
                details={
                    "code": result.code,
                    "reason": result.detail,
                    "review_required": True,
                },
            )
            return

        if result.category == "unknown":
            # An honest `unknown` is never trusted as a classification
            # hit: persist it as a failure routed to manual review.
            self._persist_classification(
                doc,
                status="failed",
                classification_type=None,
                source=result.source,
                confidence=result.confidence,
                details={
                    "rationale": result.rationale,
                    **result.details,
                    "code": "classification_uncertain",
                    "review_required": True,
                },
            )
            return

        self._persist_classification(
            doc,
            status="classified",
            classification_type=result.category,
            source=result.source,
            confidence=result.confidence,
            # Service-generated review flags merge last so result.details
            # can never shadow them.
            details={
                "rationale": result.rationale,
                **result.details,
                "review_required": False,
            },
        )

    def _persist_classification(
        self,
        doc: EvidenceDocument,
        *,
        status: str,
        classification_type: str | None,
        source: str,
        confidence: float | None,
        details: dict,
    ) -> None:
        """Persist one classification-machine advance and mirror it on
        the in-memory document (so the accepted response is truthful)."""
        advanced = self._repo.advance_classification(
            doc.document_id,
            doc.tenant_id,
            doc.property_id,
            status,
            classification_type=classification_type,
            source=source,
            confidence=confidence,
            details=json.dumps(details),
            classified_at=utcnow().isoformat(),
        )
        if advanced:
            doc.classification = (
                ClassificationStatus.CLASSIFIED
                if status == "classified"
                else ClassificationStatus.FAILED
            )
            # Mirror the persisted review fields on the in-memory document
            # (same strict mapping as the repository: `review_required`
            # must be a real bool — not int/str — and `code` / `reason`
            # must be strings; missing or wrong-typed fields stay None,
            # and the whole details object is never exposed).
            review_required = details.get("review_required")
            doc.review_required = (
                review_required if isinstance(review_required, bool) else None
            )
            code = details.get("code")
            doc.review_code = code if isinstance(code, str) else None
            reason = details.get("reason")
            doc.review_reason = reason if isinstance(reason, str) else None

    # -- property-scoped reads ---------------------------------------------------

    def list_documents(self, property_id: str, auth: AuthContext) -> list[EvidenceDocumentRef]:
        self._check_scope(property_id, auth)
        return self._repo.list_documents(auth.tenant_id, auth.property_id)

    def get_document(self, document_id: str, property_id: str, auth: AuthContext) -> EvidenceDocument:
        self._check_scope(property_id, auth)
        doc = self._repo.get_document(document_id, auth.tenant_id, auth.property_id)
        if doc is None:
            raise EvidenceScopeError("not_found", f"No document '{document_id}' in scope.")
        return doc

    def download(self, document_id: str, property_id: str, auth: AuthContext) -> DownloadPayload:
        self._check_scope(property_id, auth)
        content = self._repo.read_original(document_id, auth.tenant_id, auth.property_id)
        if content is None:
            raise EvidenceScopeError("not_found", f"No document '{document_id}' in scope.")
        doc = self._repo.get_document(document_id, auth.tenant_id, auth.property_id)
        assert doc is not None  # content exists only for an in-scope document
        return DownloadPayload(document=doc, content=content)

    # -- property-scoped text extraction (READ-ONLY) ---------------------------

    def extract_text(
        self, document_id: str, property_id: str, auth: AuthContext
    ) -> ExtractTextResult:
        """Property-scoped PDF text extraction.

        Reuses the scope check and the repository's scoped
        `get_document` / `read_original` interfaces (same as download).
        Strictly read-only: nothing is persisted, the classification
        machine is never advanced here (its state is whatever the
        post-upload run already recorded), association stays
        `unassociated`, and a non-extractable document (non-PDF /
        corrupt / encrypted / no-text) is an explicit manual-review
        outcome that never deletes or modifies the stored original.
        """
        self._check_scope(property_id, auth)
        doc = self._repo.get_document(document_id, auth.tenant_id, auth.property_id)
        if doc is None:
            raise EvidenceScopeError("not_found", f"No document '{document_id}' in scope.")
        content = self._repo.read_original(document_id, auth.tenant_id, auth.property_id)
        if content is None:
            raise EvidenceScopeError("not_found", f"No document '{document_id}' in scope.")

        if doc.file_type != "pdf":
            # Explicit manual outcome (NOT an HTTP error), mirroring the
            # PDF failure cases: the document stays intact.
            return TextManualReviewRequired(
                document_id=doc.document_id,
                storage_ref=doc.storage_ref,
                code=ManualReviewCode.FILE_TYPE_NOT_SUPPORTED,
                reason=(
                    f"Text extraction currently supports PDF documents only; "
                    f"this document is a '{doc.file_type}'. The original is "
                    "unchanged and remains downloadable."
                ),
                retryable=False,
            )

        outcome = extract_pdf_text(content)
        if outcome.ok:
            return TextExtracted(
                document_id=doc.document_id,
                storage_ref=doc.storage_ref,
                text=outcome.text,
                page_count=outcome.page_count,
            )
        assert outcome.code is not None
        return TextManualReviewRequired(
            document_id=doc.document_id,
            storage_ref=doc.storage_ref,
            code=outcome.code,
            reason=_MANUAL_REASONS[outcome.code],
            retryable=False,
        )

    # -- property-scoped manager review ---------------------------------------

    def review(self, cmd: EvidenceReviewCommand, auth: AuthContext) -> EvidenceReviewResult:
        """Settle one manager review of a document's classification
        outcome (confirm the machine's suggestion, or pick a category).

        `confirm_suggested` re-reads the machine outcome IN SCOPE from
        `get_classification` and only accepts a `classified` status whose
        category is one of the five business categories — an honest
        `unknown` (persisted as `failed`/NULL), an empty category, or any
        `failed` outcome is rejected with
        `EvidenceReviewRejectedError` and nothing is written. A settled
        human final category from a prior review is NEVER treated as the
        machine's suggestion.

        Identity: manager review is manager-only — the server-side
        identity must carry a `manager_id` verified against
        `manager_property` (default deny, no tenant fallback).
        `reviewed_by` is that verified `manager_id`, never `tenant_id`.

        The reviewer is never taken from the command; `save_review`
        appends the history and returns the settled record (a duplicate
        submission returns the existing record, preserving its original
        `reviewed_at`). The machine's own result columns are untouched —
        `save_review` only flips the details `review_required` flag.
        """
        manager_id = self._check_manager_scope(cmd.property_id, auth)
        # Manager read: not filtered by the manager's tenant_id. The repo
        # re-asserts manager_property access (already verified above) and
        # scopes only by document_id + property_id, returning None when the
        # document is not under this property.
        doc = self._repo.get_document_for_manager(
            cmd.document_id, cmd.property_id, manager_id
        )
        if doc is None:
            raise EvidenceScopeError(
                "not_found", f"No document '{cmd.document_id}' in scope."
            )

        if isinstance(cmd, ReviewConfirmSuggested):
            # Scope by the document's OWN tenant/property (from the DB row),
            # never the manager's auth.tenant_id.
            machine = self._repo.get_classification(
                cmd.document_id, doc.tenant_id, doc.property_id
            )
            if machine is None:
                raise EvidenceReviewRejectedError(
                    f"document {cmd.document_id!r} has no classification record"
                )
            status, classification_type = machine
            try:
                category = DocumentCategory(classification_type)
            except ValueError:
                category = None
            if status != ClassificationStatus.CLASSIFIED.value or category is None:
                raise EvidenceReviewRejectedError(
                    f"document {cmd.document_id!r} has no confirmable machine "
                    f"suggestion (machine status: {status!r}, category: "
                    f"{classification_type!r})"
                )
        else:  # ReviewSelectCategory
            category = cmd.category

        # Verified manager identity (see docstring): the manager_id that
        # passed `_check_manager_scope`, never the tenant_id.
        reviewed_by = manager_id
        # Scope the write by the document's OWN tenant/property (from the
        # DB row), never the manager's auth.tenant_id.
        _, rec = self._repo.save_review(
            cmd.document_id,
            doc.tenant_id,
            doc.property_id,
            cmd.status,
            category.value,
            reviewed_by,
        )
        # `rec` is the persisted record either way: for a duplicate it is
        # the EXISTING row, so its original reviewed_at is preserved.
        return EvidenceReviewResult(
            document_id=cmd.document_id,
            final_category=DocumentCategory(rec["final_category"]),
            reviewed_by=rec["reviewed_by"],
            reviewed_at=datetime.fromisoformat(rec["reviewed_at"]),
        )
