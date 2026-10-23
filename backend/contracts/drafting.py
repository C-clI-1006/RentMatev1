"""Drafting and manager review contract (guide 13.2).

Rules:
- A draft is versioned. A manager approval is bound to a SPECIFIC
  `draft_version` (plus any manager-edit final content). The sending
  path for review-required replies may only read the approved version.
  PROPOSAL: routine AUTO_ELIGIBLE replies use the exact policy-evaluated
  version without a fabricated approval. Recheck scope and policy before
  dispatch; no model re-generation after authorisation. System-generated
  rent/lease reminders always require manager approval.
- Rejection means NO tenant message. Rejections must be visible to the
  manager UI with the reason.
- `tone_hint` (e.g. from optional sentiment detection) may influence
  tone only. It can never weaken review or authorisation rules.
- `source_facts` records what grounded the draft (traceability).
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field

from .common import DraftId, MessageId, RequestId, ReviewId
from .lease_rent import FactReference


class DraftStatus(str, Enum):
    # PROPOSAL (FR-COM-04/05): `AUTO_ELIGIBLE` marks a draft whose deterministic
    # policy found no review trigger. It is an ELIGIBILITY marker only —
    # it is not a delivery mechanism. Dispatch to the tenant still flows
    # through the shared notification path (audit, dedup, permission
    # re-check). No manager review record exists for these drafts.
    AUTO_ELIGIBLE = "auto_eligible"
    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"  # a newer version replaced this one
    SENT = "sent"  # delivery confirmed (set by notification path)


class Draft(BaseModel):
    draft_id: DraftId
    message_id: MessageId
    request_id: RequestId | None = None
    version: int = 1
    audience: Literal["tenant", "manager"] = "tenant"
    content: str
    tone_hint: str | None = None  # informational only (e.g. "empathetic")
    source_facts: list[FactReference] = Field(default_factory=list)
    status: DraftStatus = DraftStatus.PENDING_REVIEW
    created_at: datetime  # UTC


class ReviewRequest(BaseModel):
    """What the manager sees: original message, grounded facts, draft,
    and why review is required."""

    review_id: ReviewId
    draft: Draft
    original_message: str
    reason: str  # e.g. "first contact about this request" / "urgent"
    source_facts: list[FactReference] = Field(default_factory=list)


class ReviewDecision(str, Enum):
    APPROVED = "approved"
    REJECTED = "rejected"


class ReviewDecisionRecord(BaseModel):
    """Immutable audit record of a decision.

    `final_content` is the exact text that may be delivered: the
    manager's edited version when edited, otherwise the approved
    draft's content. Delivery must use this record only.
    """

    review_id: ReviewId
    draft_id: DraftId
    draft_version: int  # approval is bound to this exact version
    decision: ReviewDecision
    manager_id: str
    final_content: Optional[str] = None  # None when rejected
    reason: str | None = None  # required when rejected
    decided_at: datetime  # UTC
