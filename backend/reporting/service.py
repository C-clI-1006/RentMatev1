"""Monthly report generation, deterministic derivation and idempotency."""

from __future__ import annotations

from collections.abc import Callable
from datetime import timezone

from contracts.maintenance import MaintenanceStatus, Priority
from contracts.reporting import (
    CallerIdentity,
    GenerationRequest,
    GenerationResult,
    ManagerReportTrigger,
    NarrativeSource,
    OpenTaskRef,
    OpenTasksSectionData,
    ReportGenerated,
    ReportGenerationError,
    ReportNarrative,
    ReportOutcome,
    ReportRecord,
    RiskItem,
    RiskRule,
    RisksSectionData,
    ScheduledReportTrigger,
    SectionAvailability,
)
from permissions import SQLiteManagerRepository, require_manager_property_access
from shared import new_id, utcnow

from .collector import CollectedReportData, ReportDataCollector
from .repository import ReportRepository

NarrativeGenerator = Callable[[CollectedReportData], ReportNarrative]
_TERMINAL = {MaintenanceStatus.CLOSED, MaintenanceStatus.CANCELLED}


class ReportService:
    def __init__(
        self,
        repository: ReportRepository,
        collector: ReportDataCollector,
        narrative_generator: NarrativeGenerator | None = None,
    ) -> None:
        self._repository = repository
        self._collector = collector
        self._narrative_generator = narrative_generator

    def generate(
        self, request: GenerationRequest, caller: CallerIdentity
    ) -> GenerationResult:
        if not self._authorised(request, str(caller)):
            return ReportGenerationError(
                code="authorization_denied", detail="caller is not authorised for this property"
            )

        existing = self._repository.by_event(str(request.trigger.event_id))
        if existing is not None:
            if (
                str(existing.property_id) != str(request.property_id)
                or existing.period != request.period
            ):
                return ReportGenerationError(
                    code="event_id_conflict",
                    detail="event id belongs to a different property or period",
                )
            if existing.outcome is ReportOutcome.FAILED:
                return ReportGenerationError(
                    code="all_sources_unavailable",
                    detail="maintenance, rent and lease data are unavailable",
                )
            return ReportGenerated(report=existing, reused=True)

        collected = self._collector.collect(request.property_id, request.period)
        unavailable = sum(
            section.availability is SectionAvailability.NOT_AVAILABLE
            for section in (collected.maintenance, collected.rent, collected.lease)
        )
        all_unavailable = unavailable == 3
        narrative = self._narrative(collected)
        outcome = (
            ReportOutcome.FAILED
            if all_unavailable
            else ReportOutcome.PARTIAL_SUCCESS
            if unavailable or narrative.source is NarrativeSource.MISSING
            else ReportOutcome.COMPLETE
        )
        open_tasks = _open_tasks(collected)
        risks = _risks(collected)
        now = utcnow().astimezone(timezone.utc)
        record = ReportRecord(
            report_id=new_id(),
            property_id=request.property_id,
            period=request.period,
            generation=1,  # repository assigns the atomic per-period generation
            trigger=request.trigger,
            event_id=request.trigger.event_id,
            outcome=outcome,
            maintenance=collected.maintenance,
            rent=collected.rent,
            lease=collected.lease,
            open_tasks=open_tasks,
            risks=risks,
            narrative=narrative,
            data_as_of=collected.collected_at,
            created_at=now,
            created_by=(
                "system:scheduler"
                if isinstance(request.trigger, ScheduledReportTrigger)
                else "manager:" + request.trigger.manager_id
            ),
        )
        stored = self._repository.insert(record)
        if stored.outcome is ReportOutcome.FAILED:
            return ReportGenerationError(
                code="all_sources_unavailable",
                detail="maintenance, rent and lease data are unavailable",
            )
        return ReportGenerated(report=stored, reused=stored.report_id != record.report_id)

    def _authorised(self, request: GenerationRequest, caller: str) -> bool:
        if isinstance(request.trigger, ScheduledReportTrigger):
            return caller == "system:scheduler" and self._repository.property_exists(
                str(request.property_id)
            )
        if caller != request.trigger.manager_id:
            return False
        try:
            require_manager_property_access(
                request.trigger.manager_id,
                request.property_id,
                SQLiteManagerRepository(self._repository.connection),
            )
            return True
        except PermissionError:
            return False

    def _narrative(self, collected: CollectedReportData) -> ReportNarrative:
        if self._narrative_generator is None:
            return ReportNarrative(source=NarrativeSource.MISSING)
        try:
            narrative = self._narrative_generator(collected)
            if not isinstance(narrative, ReportNarrative):
                raise TypeError("invalid narrative result")
            return narrative
        except Exception:
            return ReportNarrative(source=NarrativeSource.MISSING)


def _derived_availability(data: CollectedReportData, count: int) -> SectionAvailability:
    if all(
        section.availability is SectionAvailability.NOT_AVAILABLE
        for section in (data.maintenance, data.rent, data.lease)
    ):
        return SectionAvailability.NOT_AVAILABLE
    return SectionAvailability.AVAILABLE if count else SectionAvailability.EMPTY


def _derived_note(data: CollectedReportData, availability: SectionAvailability) -> str | None:
    missing = [
        name
        for name, section in (
            ("maintenance", data.maintenance),
            ("rent", data.rent),
            ("lease", data.lease),
        )
        if section.availability is SectionAvailability.NOT_AVAILABLE
    ]
    if availability is SectionAvailability.NOT_AVAILABLE:
        return "all contributing sources are unavailable"
    return "unavailable sources: " + ", ".join(missing) if missing else None


def _open_tasks(data: CollectedReportData) -> OpenTasksSectionData:
    items: list[OpenTaskRef] = []
    for request in data.maintenance.requests:
        if request.status not in _TERMINAL:
            items.append(
                OpenTaskRef(
                    source="maintenance",
                    source_id=str(request.request_id),
                    summary=f"Maintenance request remains {request.status.value}",
                    since=request.created_at,
                    references=request.references,
                )
            )
    for rent in data.rent.records:
        if rent.status != "paid":
            items.append(
                OpenTaskRef(
                    source="rent",
                    source_id=str(rent.rent_record_id),
                    summary=f"Rent status is {rent.status}",
                    references=rent.references,
                )
            )
    for lease in data.lease.leases:
        if lease.status in {"expiring", "unknown"}:
            items.append(
                OpenTaskRef(
                    source="lease",
                    source_id=str(lease.lease_id),
                    summary=f"Lease status is {lease.status}",
                    references=lease.references,
                )
            )
    availability = _derived_availability(data, len(items))
    return OpenTasksSectionData(
        availability=availability,
        items=items,
        note=_derived_note(data, availability),
    )


def _risks(data: CollectedReportData) -> RisksSectionData:
    risks: list[RiskItem] = []
    for request in data.maintenance.requests:
        if request.priority is Priority.EMERGENCY and request.status not in _TERMINAL:
            risks.append(
                RiskItem(
                    rule_id=RiskRule.OPEN_EMERGENCY,
                    severity="high",
                    summary="Emergency maintenance request remains open",
                    references=request.references,
                )
            )
    for rent in data.rent.records:
        if rent.status == "overdue":
            risks.append(
                RiskItem(
                    rule_id=RiskRule.OVERDUE_RENT,
                    severity="high",
                    summary="Rent record is overdue",
                    references=rent.references,
                )
            )
    for lease in data.lease.leases:
        if lease.status == "expiring":
            risks.append(
                RiskItem(
                    rule_id=RiskRule.LEASE_EXPIRING,
                    severity="medium",
                    summary="Lease expires within the reporting threshold",
                    references=lease.references,
                )
            )
    availability = _derived_availability(data, len(risks))
    return RisksSectionData(
        availability=availability,
        risks=risks,
        note=_derived_note(data, availability),
    )
