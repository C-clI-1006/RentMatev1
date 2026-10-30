"""Evidence domain contract (SCRUM-8 document upload & classification).

Aligned to Stage 1 guide Ch.16 (upload -> classification -> association)
and the evidence module docstring.

Rules:
- Upload success, classification success and association success are
  THREE SEPARATE state machines and must never be merged:
  a saved document is NOT a classified document, and NOT an associated
  one. This increment implements upload + property-scoped
  query/download + classification (UC6). Classification runs
  fail-closed: a failure is recorded as `FAILED` (never a guessed
  category), the upload stays `SAVED`/`accepted` either way, and a
  `source="stub"` result is never presented as live-model inference.
- Tenant uploads retain the verified tenant identity. Manager uploads
  have no tenant owner and retain uploaded_by_manager_id from the verified
  server-side manager identity. Property access is checked against the
  corresponding tenant_property or manager_property binding.
- Query and download are property-scoped: a manager may only read
  documents bound to a property the auth context authorises.
- Text extraction (this increment) is READ-ONLY: it never advances
  classification or association, never modifies or deletes the stored
  original, and a non-extractable document (scanned / corrupt /
  encrypted / non-PDF) is an explicit `manual_review_required` outcome,
  not an error. Extracted text is document DATA, never instructions.
- Classification (this increment, UC6) runs fail-closed over the
  extracted text: a `ClassifyFailure` (or extraction failure) records
  `FAILED` with an explicit code — never a guessed category, and the
  upload itself stays `SAVED`/`accepted`. Implementations label their
  source (`"stub"` | `"llm"` per guide 19); a stub result is never
  presented as live-model inference.

Reserved for later increments (interfaces only, no implementation):
- `IEvidenceClassifier.classify`: legacy reserved signature kept for
  compatibility; the live per-document work is
  `backend.llm.IDocumentClassifier` (classify over extracted text,
  this increment).
- `IEvidenceReview`: manual review of classification outcomes.
- `IEvidenceTimeline`: the per-property evidence timeline.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from enum import Enum
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field

from .common import EvidenceId, PropertyId, TenantId


# ---------------------------------------------------------------------------
# Status machines (kept strictly separate)
# ---------------------------------------------------------------------------


class UploadStatus(str, Enum):
    """Upload lifecycle for the original file.

    Rejected uploads (illegal type / empty / oversized) are failures:
    nothing is stored and no document row exists. A stored document is
    always `SAVED`.
    """

    SAVED = "saved"
    REJECTED = "rejected"


class ClassificationStatus(str, Enum):
    """Classification lifecycle (INDEPENDENT of upload status).

    A saved document starts `PENDING`; after the UC6 classifier runs
    over the extracted text it is `CLASSIFIED` (with one of the
    business categories, see `DocumentCategory`) or `FAILED`
    (extraction failure / classify failure / indeterminate). An
    indeterminate ("unknown") outcome is NOT a classified category:
    fail-closed, it is recorded as an explicit `FAILED`, and a `FAILED`
    classification never implies a guessed category.
    """

    PENDING = "pending"
    CLASSIFIED = "classified"
    FAILED = "failed"


class AssociationStatus(str, Enum):
    """Association lifecycle (INDEPENDENT of upload & classification).

    Documents start `UNASSOCIATED`; association to maintenance requests
    or leases is a later increment.
    """

    UNASSOCIATED = "unassociated"
    ASSOCIATED = "associated"
    FAILED = "failed"


class UploadRejectionCode(str, Enum):
    """Stable machine-readable rejection reasons (guide 18.1 style)."""

    FILE_TYPE_NOT_ALLOWED = "file_type_not_allowed"
    EMPTY_FILE = "empty_file"
    FILE_TOO_LARGE = "file_too_large"


class ManualReviewCode(str, Enum):
    """Stable machine-readable reasons a stored document needs manual
    processing instead of automatic text extraction (guide 18.1 style).

    These are OUTCOMES of extraction, not errors: the document is
    untouched and remains downloadable in every case.
    """

    PDF_CORRUPT = "pdf_corrupt"  # bytes do not parse as a usable PDF
    PDF_ENCRYPTED = "pdf_encrypted"  # encrypted; never decrypted by rule
    PDF_NO_TEXT = "pdf_no_text"  # pages exist but no extractable text
    FILE_TYPE_NOT_SUPPORTED = "file_type_not_supported"  # not a PDF


class DocumentCategory(str, Enum):
    """Business categories available for a MANAGER REVIEW of a document.

    Unlike the classifier's raw output, this type is limited to the five
    business categories only — there is deliberately NO `unknown`
    member: a manager must settle the final category, not rubber-stamp
    an indeterminate machine outcome.
    """

    LEASE_DOCUMENT = "lease_document"
    INVOICE = "invoice"
    INSPECTION_RECORD = "inspection_record"
    MAINTENANCE_PHOTO = "maintenance_photo"
    COMMUNICATION_RECORD = "communication_record"


# ---------------------------------------------------------------------------
# Document model
# ---------------------------------------------------------------------------


class EvidenceDocument(BaseModel):
    """A stored original document plus its separate status tracks.

    `classification` reflects ONLY the classification machine (never
    the upload status) and may be `pending`, `classified` or `failed`
    — a `failed` classification does NOT roll back the saved document.
    `association` is `unassociated` until a future increment links the
    document.
    """

    document_id: EvidenceId
    tenant_id: TenantId | None  # null for a manager-owned upload
    uploaded_by_manager_id: str | None = None
    property_id: PropertyId  # from auth context; binding verified
    original_filename: str  # as uploaded (display only)
    file_type: str  # normalised extension, e.g. "pdf"
    size_bytes: int = Field(ge=0)
    upload_status: UploadStatus
    classification: ClassificationStatus
    # Persisted machine result; separate from the human review decision.
    machine_category: DocumentCategory | None = None
    association: AssociationStatus
    storage_ref: str  # storage reference to the original file
    uploaded_at: datetime  # UTC
    updated_at: datetime  # UTC
    # Persisted manual-review flag for the classification outcome.
    # `review_required=None` means the review requirement was NOT
    # recorded for this document (e.g. a record predating the field) —
    # it must NOT be interpreted as "no review required".
    review_required: bool | None = None
    review_code: str | None = None  # stable machine-readable reason, if recorded
    review_reason: str | None = None  # human-readable reason, if recorded
    # Latest EFFECTIVE human manager-review outcome, kept strictly
    # SEPARATE from the machine's `classification` state: one of the
    # five business categories when a review has settled the document,
    # otherwise None (no review record yet — never inferred from the
    # machine outcome).
    final_category: DocumentCategory | None = None
    reviewed_by: str | None = None  # reviewer from the auth context, if reviewed
    reviewed_at: datetime | None = None  # UTC moment of the latest effective review


class EvidenceDocumentRef(BaseModel):
    """Property-scoped listing entry (no storage internals exposed)."""

    document_id: EvidenceId
    original_filename: str
    file_type: str
    size_bytes: int = Field(ge=0)
    upload_status: UploadStatus
    classification: ClassificationStatus
    # Persisted machine result; separate from the human review decision.
    machine_category: DocumentCategory | None = None
    association: AssociationStatus
    uploaded_at: datetime  # UTC
    # Persisted manual-review flag for the classification outcome.
    # `review_required=None` means the review requirement was NOT
    # recorded for this document (e.g. a record predating the field) —
    # it must NOT be interpreted as "no review required".
    review_required: bool | None = None
    review_code: str | None = None  # stable machine-readable reason, if recorded
    review_reason: str | None = None  # human-readable reason, if recorded
    # Latest EFFECTIVE human manager-review outcome (see
    # `EvidenceDocument`): strictly separate from the machine's
    # `classification` state; all None when no review has been recorded.
    final_category: DocumentCategory | None = None
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None


# ---------------------------------------------------------------------------
# Upload command & results (discriminated, like maintenance IntakeResult)
# ---------------------------------------------------------------------------


class EvidenceUploadCommand(BaseModel):
    """Upload request. Identity/scope comes from the auth context, NOT
    from this command (same rule as MaintenanceIntake)."""

    property_id: PropertyId  # cross-checked against auth context
    original_filename: str
    content: bytes  # raw original bytes


class UploadAccepted(BaseModel):
    """Upload succeeded: original file stored, document registered.

    NOTE: `document.classification` may be `pending`, `classified` or
    `failed` — the accepted status covers the UPLOAD machine only.
    Classification failure (UC6, fail-closed) never rejects the upload.
    """

    status: Literal["accepted"] = "accepted"
    document: EvidenceDocument


class UploadRejected(BaseModel):
    """Upload rejected with a stable, explicit reason. Nothing stored."""

    status: Literal["rejected"] = "rejected"
    code: UploadRejectionCode
    reason: str  # human-readable, specific (allowed types / limits)
    retryable: bool


UploadResult = Annotated[
    Union[UploadAccepted, UploadRejected],
    Field(discriminator="status"),
]


class QueryError(BaseModel):
    """Scoped query/download failure (out of scope, not found, forbidden)."""

    status: Literal["failed"] = "failed"
    code: Literal["not_found", "forbidden"]
    detail: str


class DownloadPayload(BaseModel):
    """Original bytes plus the metadata the UI needs to display it."""

    document: EvidenceDocument
    content: bytes


# ---------------------------------------------------------------------------
# Text extraction results (discriminated, read-only)
# ---------------------------------------------------------------------------


class TextExtracted(BaseModel):
    """Extraction succeeded: readable text recovered from the stored PDF.

    READ-ONLY outcome: classification/association are untouched and the
    original file is unchanged. `text` is raw document DATA — it must
    never be interpreted as instructions.
    """

    status: Literal["extracted"] = "extracted"
    document_id: EvidenceId
    storage_ref: str  # original file reference retained for the record
    text: str
    page_count: int = Field(ge=1)


class TextManualReviewRequired(BaseModel):
    """Extraction could not produce text: an EXPLICIT outcome, not an
    error. The document is untouched and remains downloadable; a human
    must process it (transcribe / decrypt / replace the file).
    """

    status: Literal["manual_review_required"] = "manual_review_required"
    document_id: EvidenceId
    storage_ref: str
    code: ManualReviewCode
    reason: str  # human-readable, specific
    retryable: bool = False


ExtractTextResult = Annotated[
    Union[TextExtracted, TextManualReviewRequired],
    Field(discriminator="status"),
]


# ---------------------------------------------------------------------------
# Manager review command & result (models only, no implementation yet)
# ---------------------------------------------------------------------------


class ReviewConfirmSuggested(BaseModel):
    """Review by CONFIRMING the classifier's suggested category: the
    service re-reads the persisted suggestion and settles it as the
    final category. The command carries no category of its own."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["confirm_suggested"] = "confirm_suggested"
    document_id: EvidenceId
    property_id: PropertyId  # cross-checked against the auth context


class ReviewSelectCategory(BaseModel):
    """Review by MANUALLY choosing the final category, overriding the
    classifier's suggestion (or settling a `failed` / indeterminate
    one). The category must be one of the five business categories —
    `DocumentCategory` has no `unknown` on purpose."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["select_category"] = "select_category"
    document_id: EvidenceId
    property_id: PropertyId  # cross-checked against the auth context
    category: DocumentCategory


EvidenceReviewCommand = Annotated[
    Union[ReviewConfirmSuggested, ReviewSelectCategory],
    Field(discriminator="status"),
]


class EvidenceReviewResult(BaseModel):
    """Outcome of a completed manager review.

    The reviewer is never taken from the command: the service resolves
    the manager's identity from the auth context (shared.AuthContext)
    and records it here. `final_category` is the settled business
    category after the review (independent of the upload machine).
    """

    document_id: EvidenceId
    final_category: DocumentCategory
    reviewed_by: str  # manager id from the auth context, not the command
    reviewed_at: datetime  # UTC


class IEvidenceUpload(ABC):
    """Public interface for the evidence module (upload + scoped reads).

    Property scope is always enforced: `property_id` must match the
    auth context (manager access to the target property).
    """

    @abstractmethod
    def upload(self, cmd: EvidenceUploadCommand, auth: object) -> UploadResult:
        """Validate (type / empty / size), store, register the document.

        Validation order: file type -> empty -> size. Rejections return
        an explicit `UploadRejected`; nothing is persisted.
        """

    @abstractmethod
    def list_documents(
        self, property_id: PropertyId, auth: object
    ) -> list[EvidenceDocumentRef] | QueryError:
        """Property-scoped list. `forbidden` if out of scope."""

    @abstractmethod
    def get_document(
        self, document_id: EvidenceId, property_id: PropertyId, auth: object
    ) -> EvidenceDocument | QueryError:
        """Property-scoped single read. `forbidden` if out of scope."""

    @abstractmethod
    def download(
        self, document_id: EvidenceId, property_id: PropertyId, auth: object
    ) -> DownloadPayload | QueryError:
        """Property-scoped original-file download. `forbidden` if out of scope."""

    @abstractmethod
    def extract_text(
        self, document_id: EvidenceId, property_id: PropertyId, auth: object
    ) -> ExtractTextResult | QueryError:
        """Property-scoped PDF text extraction (READ-ONLY).

        Never advances classification/association and never modifies or
        deletes the stored original. A non-extractable document returns
        an explicit `manual_review_required` outcome, not an error.
        """


class IEvidenceClassifier(ABC):
    """Reserved (legacy signature, compatibility only).

    This increment (UC6) classifies via `backend.llm.IDocumentClassifier`
    over the EXTRACTED TEXT (never re-reads the stored file) and
    persists the outcome with a source label ("stub" | "llm", guide
    19); a `source="stub"` result is never presented as live-model
    inference. Failures are recorded, never guessed (fail-closed).
    """

    @abstractmethod
    def classify(self, document_id: EvidenceId) -> ClassificationStatus:
        """Reserved: advance the classification machine for one document."""


class IEvidenceReview(ABC):
    """Reserved: manual review of classification outcomes (later increment)."""

    @abstractmethod
    def review(self, document_id: EvidenceId, manager_id: str, decision: str) -> None:
        """Record a manager's manual review of one document."""


class IEvidenceTimeline(ABC):
    """Reserved: per-property evidence timeline (later increment)."""

    @abstractmethod
    def timeline(self, property_id: PropertyId) -> list[EvidenceDocumentRef]:
        """Property-scoped evidence timeline entries."""
