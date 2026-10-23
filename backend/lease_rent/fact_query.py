"""Implementation of the shared authorised lease/rent fact-query contract."""

from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Protocol

from contracts.common import LeaseId
from contracts.lease_rent import (
    Fact,
    FactAvailability,
    FactReference,
    ILeaseRentQuery,
    LeaseRentFactResult,
    LeaseRentQuery,
    QuestionType,
)

from .models import VerifiedFactRecord


class LeaseRentFactRepository(Protocol):
    """Internal data-access port; the SQLite adapter can be added later."""

    def is_authorized(self, *, tenant_id: str, property_id: str) -> bool:
        """Return whether the tenant is bound to the requested property."""

    def find_facts(
        self,
        *,
        tenant_id: str,
        property_id: str,
        question_type: QuestionType,
        as_of: datetime,
    ) -> Sequence[VerifiedFactRecord]:
        """Return verified records applicable to the requested time."""


class LeaseRentQueryService(ILeaseRentQuery):
    """Return authorised, source-backed facts without free generation."""

    def __init__(self, repository: LeaseRentFactRepository) -> None:
        self._repository = repository

    def get_facts(self, query: LeaseRentQuery) -> LeaseRentFactResult:
        _require_utc(query.as_of, field_name="query.as_of")

        if not self._repository.is_authorized(
            tenant_id=str(query.tenant_id), property_id=str(query.property_id)
        ):
            return LeaseRentFactResult(
                message_id=query.message_id,
                availability=FactAvailability.AUTHORIZATION_DENIED,
                note="The tenant is not authorized for the requested property.",
            )

        if query.question_type is QuestionType.OTHER:
            return LeaseRentFactResult(
                message_id=query.message_id,
                availability=FactAvailability.NOT_AVAILABLE,
                note="This question is outside the verified lease/rent fact categories.",
            )

        records = self._repository.find_facts(
            tenant_id=str(query.tenant_id),
            property_id=str(query.property_id),
            question_type=query.question_type,
            as_of=query.as_of,
        )
        eligible = _eligible_records(records, query=query)
        if not eligible:
            return LeaseRentFactResult(
                message_id=query.message_id,
                availability=FactAvailability.NOT_AVAILABLE,
                note="No verified evidence is available at the requested time.",
            )

        latest_by_name = _latest_records_by_name(eligible)
        if _has_value_conflict(latest_by_name):
            return LeaseRentFactResult(
                message_id=query.message_id,
                availability=FactAvailability.CONFLICT,
                note="Verified sources disagree; human review is required.",
            )

        selected = [record for records in latest_by_name.values() for record in records]
        lease_ids = {record.lease_id for record in selected if record.lease_id is not None}
        if len(lease_ids) > 1:
            return LeaseRentFactResult(
                message_id=query.message_id,
                availability=FactAvailability.CONFLICT,
                note="Multiple applicable leases were found; human review is required.",
            )

        facts = [
            _to_contract_fact(name, records, query.as_of)
            for name, records in sorted(latest_by_name.items())
        ]
        lease_id = LeaseId(next(iter(lease_ids))) if lease_ids else None
        return LeaseRentFactResult(
            message_id=query.message_id,
            availability=FactAvailability.AVAILABLE,
            facts=facts,
            lease_id=lease_id,
        )


def _eligible_records(
    records: Sequence[VerifiedFactRecord], *, query: LeaseRentQuery
) -> tuple[VerifiedFactRecord, ...]:
    eligible: list[VerifiedFactRecord] = []
    for record in records:
        _require_utc(record.observed_at, field_name="record.observed_at")
        if record.observed_at > query.as_of:
            continue
        if query.question_type in {
            QuestionType.RENT_STATUS,
            QuestionType.RENT_AMOUNT,
        } and record.source_type != "rent_record":
            continue
        eligible.append(record)
    return tuple(eligible)


def _latest_records_by_name(
    records: Sequence[VerifiedFactRecord],
) -> dict[str, tuple[VerifiedFactRecord, ...]]:
    grouped: dict[str, list[VerifiedFactRecord]] = defaultdict(list)
    for record in records:
        grouped[record.name].append(record)

    latest: dict[str, tuple[VerifiedFactRecord, ...]] = {}
    for name, values in grouped.items():
        latest_time = max(record.observed_at for record in values)
        latest[name] = tuple(
            record for record in values if record.observed_at == latest_time
        )
    return latest


def _has_value_conflict(
    records_by_name: dict[str, tuple[VerifiedFactRecord, ...]],
) -> bool:
    return any(
        len({record.value for record in records}) > 1
        for records in records_by_name.values()
    )


def _to_contract_fact(
    name: str, records: tuple[VerifiedFactRecord, ...], as_of: datetime
) -> Fact:
    first = records[0]
    references = [
        FactReference(
            source_type=record.source_type,
            source_id=record.source_id,
            version=record.version,
            snippet=record.snippet,
        )
        for record in sorted(records, key=lambda item: (item.source_type, item.source_id))
    ]
    return Fact(
        name=name,
        value=first.value,
        as_of=first.observed_at,
        references=references,
        stale=all(_is_stale(record, as_of=as_of) for record in records),
    )


def _is_stale(record: VerifiedFactRecord, *, as_of: datetime) -> bool:
    if record.stale_after is None:
        return False
    return record.observed_at + record.stale_after < as_of


def _require_utc(value: datetime, *, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{field_name} must be timezone-aware UTC")
