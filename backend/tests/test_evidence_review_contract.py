"""Contract tests for the NEW manager-review models (no service layer yet).

Run: pytest backend/tests/test_evidence_review_contract.py

Covers the additions to `contracts.evidence` only:
- `DocumentCategory` accepts exactly the five business categories and
  rejects "unknown";
- `EvidenceReviewCommand` discriminates on `status` and parses each
  variant into the right model;
- `select_category` without a `category` fails;
- `confirm_suggested` carrying a `category`, and any command carrying a
  self-reported `manager_id`, are REJECTED (identity/scope comes from
  the auth context, never from the command).
"""

from datetime import datetime, timezone

import pytest
from pydantic import TypeAdapter, ValidationError

from contracts.evidence import (
    DocumentCategory,
    EvidenceReviewCommand,
    EvidenceReviewResult,
    ReviewConfirmSuggested,
    ReviewSelectCategory,
)

_COMMAND = TypeAdapter(EvidenceReviewCommand)

FIVE_CATEGORIES = (
    "lease_document",
    "invoice",
    "inspection_record",
    "maintenance_photo",
    "communication_record",
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def test_document_category_accepts_exactly_the_five_business_categories():
    assert len(DocumentCategory) == 5
    for value in FIVE_CATEGORIES:
        assert DocumentCategory(value) is not None
        parsed = _COMMAND.validate_python(
            {"status": "select_category",
             "document_id": "doc-1", "property_id": "prop-1", "category": value}
        )
        assert isinstance(parsed, ReviewSelectCategory)
        assert parsed.category.value == value
    # "unknown" is deliberately NOT a business category: a direct enum
    # lookup raises the enum's native ValueError.
    with pytest.raises(ValueError):
        DocumentCategory("unknown")
    with pytest.raises(ValidationError):
        _COMMAND.validate_python(
            {"status": "select_category",
             "document_id": "doc-1", "property_id": "prop-1", "category": "unknown"}
        )


def test_command_discriminated_union_parses_by_status():
    confirm = _COMMAND.validate_python(
        {"status": "confirm_suggested", "document_id": "doc-2", "property_id": "prop-1"}
    )
    assert isinstance(confirm, ReviewConfirmSuggested)
    assert "category" not in type(confirm).model_fields

    select = _COMMAND.validate_python(
        {"status": "select_category",
         "document_id": "doc-3", "property_id": "prop-1",
         "category": "maintenance_photo"}
    )
    assert isinstance(select, ReviewSelectCategory)
    assert select.category is DocumentCategory.MAINTENANCE_PHOTO

    with pytest.raises(ValidationError):
        _COMMAND.validate_python(
            {"status": "bogus_status", "document_id": "doc-4", "property_id": "prop-1"}
        )


def test_select_category_requires_category():
    with pytest.raises(ValidationError) as excinfo:
        _COMMAND.validate_python(
            {"status": "select_category", "document_id": "doc-5", "property_id": "prop-1"}
        )
    # Pydantic reports the missing field under the discriminated-union
    # path: loc == ("select_category", "category").
    assert any(
        e["type"] == "missing" and e["loc"] == ("select_category", "category")
        for e in excinfo.value.errors()
    )


def test_confirm_suggested_must_not_carry_category():
    with pytest.raises(ValidationError):
        _COMMAND.validate_python(
            {"status": "confirm_suggested", "document_id": "doc-6",
             "property_id": "prop-1", "category": "invoice"}
        )


@pytest.mark.parametrize("status", ["confirm_suggested", "select_category"])
def test_commands_must_not_carry_self_reported_manager_id(status):
    payload = (
        {"status": "confirm_suggested", "document_id": "doc-7", "property_id": "prop-1"}
        if status == "confirm_suggested"
        else {"status": "select_category", "document_id": "doc-7",
               "property_id": "prop-1", "category": "invoice"}
    )
    payload["manager_id"] = "mgr-self-reported"
    # The reviewer identity comes from the auth context, so a
    # self-reported manager_id in the command must be rejected.
    with pytest.raises(ValidationError):
        _COMMAND.validate_python(payload)


def test_review_result_carries_settled_category_and_reviewer():
    result = EvidenceReviewResult(
        document_id="doc-8",
        final_category=DocumentCategory.LEASE_DOCUMENT,
        reviewed_by="mgr-1",
        reviewed_at=_now(),
    )
    assert result.final_category is DocumentCategory.LEASE_DOCUMENT
    assert result.reviewed_by == "mgr-1"
