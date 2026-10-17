"""Communication -> Lease & Rent contract (guide 15.x, 18.1).

Caller: communication module (Chaney).
Provider: lease_rent module (Ziheng).

Rules:
- The provider performs the authorisation check: facts are returned
  only for the tenant/property pair in the query, and only from
  authorised sources.
- Current payment status must come from structured rent records, not
  from RAG over lease documents (guide 19.2).
- "No evidence found" must NOT be reported as "the rule does not
  exist" — use `FactAvailability.NOT_AVAILABLE` with a note.
- `as_of` pins the fact validity time; stale facts must be flagged.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field

from .common import LeaseId, MessageId, PropertyId, TenantId


class QuestionType(str, Enum):
    RENT_STATUS = "rent_status"  # current payment status -> rent records
    RENT_AMOUNT = "rent_amount"
    LEASE_TERMS = "lease_terms"  # lease clauses -> documents
    LEASE_DATES = "lease_dates"
    OTHER = "other"


class LeaseRentQuery(BaseModel):
    """An authorised fact request produced by communication."""

    message_id: MessageId
    tenant_id: TenantId  # from auth context
    property_id: PropertyId  # from auth context; binding verified
    question_type: QuestionType
    question: str
    as_of: datetime  # UTC; pins fact validity


class FactAvailability(str, Enum):
    AVAILABLE = "available"
    NOT_AVAILABLE = "not_available"  # e.g. no evidence found
    AUTHORIZATION_DENIED = "authorization_denied"
    CONFLICT = "conflict"  # sources disagree; needs human handling


class FactReference(BaseModel):
    """Source provenance for a fact — replies must be traceable to this."""

    source_type: str  # "lease_document" | "rent_record" | "system"
    source_id: str  # e.g. LeaseId or RentRecordId
    version: str | None = None  # document/record version
    snippet: str | None = None  # short excerpt used as evidence


class Fact(BaseModel):
    name: str  # machine-readable key, e.g. "current_period"
    value: str  # human-readable value
    as_of: datetime  # UTC
    references: list[FactReference] = Field(default_factory=list)
    stale: bool = False


class LeaseRentFactResult(BaseModel):
    message_id: MessageId
    availability: FactAvailability
    facts: list[Fact] = Field(default_factory=list)
    note: str | None = None  # e.g. "no rent record for this period yet"
    lease_id: LeaseId | None = None


# Backwards-friendly alias used by the communication module.
FactResult = LeaseRentFactResult


class ILeaseRentQuery(ABC):
    """Public interface for the lease & rent module."""

    @abstractmethod
    def get_facts(self, query: LeaseRentQuery) -> LeaseRentFactResult:
        """Return verified facts with source references, or an
        authoritative 'not available' / 'denied' / 'conflict' outcome.
        Must never fabricate facts to satisfy the caller.
        """
