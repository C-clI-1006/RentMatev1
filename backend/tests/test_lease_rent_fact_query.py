"""Tests for the shared ILeaseRentQuery implementation."""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import pytest

from contracts.common import MessageId, PropertyId, TenantId
from contracts.lease_rent import FactAvailability, LeaseRentQuery, QuestionType
from lease_rent import LeaseRentQueryService, VerifiedFactRecord


NOW = datetime(2026, 10, 20, 8, 0, tzinfo=timezone.utc)


@dataclass
class FakeFactRepository:
    authorized: bool = True
    records: list[VerifiedFactRecord] = field(default_factory=list)
    find_calls: int = 0

    def is_authorized(self, *, tenant_id: str, property_id: str) -> bool:
        return self.authorized

    def find_facts(self, **_kwargs) -> list[VerifiedFactRecord]:
        self.find_calls += 1
        return self.records


def _query(question_type: QuestionType, *, as_of: datetime = NOW) -> LeaseRentQuery:
    return LeaseRentQuery(
        message_id=MessageId("message-1"),
        tenant_id=TenantId("tenant-1"),
        property_id=PropertyId("property-1"),
        question_type=question_type,
        question="What is my current rent status?",
        as_of=as_of,
    )


def _record(
    *,
    name: str = "current_period_status",
    value: str = "paid",
    observed_at: datetime = NOW,
    source_type: str = "rent_record",
    source_id: str = "rent-1",
    lease_id: str = "lease-1",
    stale_after: timedelta | None = timedelta(days=1),
) -> VerifiedFactRecord:
    return VerifiedFactRecord(
        name=name,
        value=value,
        observed_at=observed_at,
        source_type=source_type,
        source_id=source_id,
        lease_id=lease_id,
        version="1",
        stale_after=stale_after,
    )


def test_authorized_rent_status_returns_source_backed_fact():
    repository = FakeFactRepository(records=[_record()])
    result = LeaseRentQueryService(repository).get_facts(
        _query(QuestionType.RENT_STATUS)
    )

    assert result.availability is FactAvailability.AVAILABLE
    assert result.lease_id == "lease-1"
    assert result.facts[0].name == "current_period_status"
    assert result.facts[0].value == "paid"
    assert result.facts[0].references[0].source_type == "rent_record"
    assert result.facts[0].references[0].source_id == "rent-1"
    assert not result.facts[0].stale


def test_unauthorized_query_returns_no_facts_and_does_not_read_records():
    repository = FakeFactRepository(authorized=False, records=[_record()])
    result = LeaseRentQueryService(repository).get_facts(
        _query(QuestionType.RENT_STATUS)
    )

    assert result.availability is FactAvailability.AUTHORIZATION_DENIED
    assert result.facts == []
    assert repository.find_calls == 0


def test_missing_evidence_is_not_reported_as_rule_absence():
    result = LeaseRentQueryService(FakeFactRepository()).get_facts(
        _query(QuestionType.LEASE_TERMS)
    )

    assert result.availability is FactAvailability.NOT_AVAILABLE
    assert result.facts == []
    assert "No verified evidence" in result.note


def test_rent_status_rejects_document_or_rag_evidence():
    repository = FakeFactRepository(
        records=[_record(source_type="lease_document", source_id="doc-1")]
    )
    result = LeaseRentQueryService(repository).get_facts(
        _query(QuestionType.RENT_STATUS)
    )

    assert result.availability is FactAvailability.NOT_AVAILABLE
    assert result.facts == []


def test_stale_fact_is_returned_but_explicitly_flagged():
    old = NOW - timedelta(days=5)
    repository = FakeFactRepository(records=[_record(observed_at=old)])
    result = LeaseRentQueryService(repository).get_facts(
        _query(QuestionType.RENT_STATUS)
    )

    assert result.availability is FactAvailability.AVAILABLE
    assert result.facts[0].stale


def test_latest_fact_supersedes_older_value_without_false_conflict():
    repository = FakeFactRepository(
        records=[
            _record(value="unpaid-but-not-due", observed_at=NOW - timedelta(days=1)),
            _record(value="paid", observed_at=NOW, source_id="rent-2"),
        ]
    )
    result = LeaseRentQueryService(repository).get_facts(
        _query(QuestionType.RENT_STATUS)
    )

    assert result.availability is FactAvailability.AVAILABLE
    assert result.facts[0].value == "paid"
    assert result.facts[0].references[0].source_id == "rent-2"


def test_disagreement_at_same_latest_time_requires_human_review():
    repository = FakeFactRepository(
        records=[
            _record(value="paid", source_id="rent-1"),
            _record(value="overdue", source_id="rent-2"),
        ]
    )
    result = LeaseRentQueryService(repository).get_facts(
        _query(QuestionType.RENT_STATUS)
    )

    assert result.availability is FactAvailability.CONFLICT
    assert result.facts == []
    assert "human review" in result.note


def test_other_question_does_not_trigger_unverified_lookup():
    repository = FakeFactRepository(records=[_record()])
    result = LeaseRentQueryService(repository).get_facts(_query(QuestionType.OTHER))

    assert result.availability is FactAvailability.NOT_AVAILABLE
    assert repository.find_calls == 0


def test_future_observation_is_not_used_for_earlier_as_of_query():
    repository = FakeFactRepository(records=[_record(observed_at=NOW)])
    result = LeaseRentQueryService(repository).get_facts(
        _query(QuestionType.RENT_STATUS, as_of=NOW - timedelta(minutes=1))
    )

    assert result.availability is FactAvailability.NOT_AVAILABLE


def test_naive_as_of_timestamp_is_rejected():
    with pytest.raises(ValueError, match="query.as_of"):
        LeaseRentQueryService(FakeFactRepository()).get_facts(
            _query(QuestionType.RENT_STATUS, as_of=NOW.replace(tzinfo=None))
        )
