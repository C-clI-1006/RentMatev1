"""Internal data collection for the Stage 1 monthly report (UC7).

Gathers the three data-carrying sections (maintenance / rent / lease)
for one (property, period) through the two EXISTING source interfaces
only (module-boundary rule — no direct table reads):

- ``IMaintenanceReportSource.list_for_period``  (maintenance module)
- ``ILeaseRentReportSource.period_summary``     (lease & rent module)

Scope of this round:
- Read-only: the collector never mutates source records.
- No LLM: collection is pure data; narratives are produced later,
  elsewhere (and the LLM never invents facts).
- No report-level judgement: the collector does NOT decide whether the
  whole report is generated (no ``ReportOutcome``). It only preserves
  each section's own availability (AVAILABLE / EMPTY / NOT_AVAILABLE)
  and its unavailability note, so EMPTY ("本周期无记录") and
  NOT_AVAILABLE ("数据不可用") stay distinct all the way downstream.

Failure handling: the contract says a source encodes data-level
problems in the section availability and "must not raise". As a
defence against unexpected source errors, the collector maps a raised
exception to NOT_AVAILABLE + a SHORT GENERIC note for that source's
sections — one source failing must not kill the other sections.
Exception details are logged internally (logger) and never surfaced
in the externally rendered note.

The one exception: a ``PermissionError`` from either source is
PROPAGATED unchanged (no collection result is returned at all).
Permission denial is an authorisation failure, not a data gap — it
must be handled by the service layer above, never silently degraded
into NOT_AVAILABLE ("data unavailable").
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from pydantic import BaseModel, Field

from contracts.common import PropertyId
from contracts.reporting import (
    ILeaseRentReportSource,
    IMaintenanceReportSource,
    LeaseSectionData,
    MaintenanceSectionData,
    RentSectionData,
    ReportPeriod,
    SectionAvailability,
)

logger = logging.getLogger(__name__)

# Short, generic, externally safe notes for defensively degraded sections.
# Exception details stay in the internal log (logger.exception), never in
# these notes.
_MAINTENANCE_SOURCE_NOTE = "maintenance data source temporarily unavailable"
_LEASE_RENT_SOURCE_NOTE = "lease & rent data source temporarily unavailable"


class CollectedReportData(BaseModel):
    """Collected data sections for one (property, period).

    Deliberately carries NO report-level outcome and NO narrative:
    judging whether the report is generated belongs to the report
    service, not the collector. Each section keeps its own
    availability and note as returned (or defensively derived) from
    its source.
    """

    property_id: PropertyId
    period: ReportPeriod
    maintenance: MaintenanceSectionData
    rent: RentSectionData
    lease: LeaseSectionData
    # UTC moment the collection read its sources. Per contract Q11 this
    # is NOT a same-instant snapshot claim: sources are read
    # sequentially, each reflecting its own read time.
    collected_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ReportDataCollector:
    """Collects the three data sections for one (property, period).

    Uses only the two source interfaces above; no LLM, no report-level
    completeness judgement. Each source is consulted at most once per
    ``collect`` call.
    """

    def __init__(
        self,
        maintenance_source: IMaintenanceReportSource,
        lease_rent_source: ILeaseRentReportSource,
    ) -> None:
        self._maintenance_source = maintenance_source
        self._lease_rent_source = lease_rent_source

    def collect(
        self, property_id: PropertyId, period: ReportPeriod
    ) -> CollectedReportData:
        maintenance = self._collect_maintenance(property_id, period)
        rent, lease = self._collect_lease_rent(property_id, period)
        return CollectedReportData(
            property_id=property_id,
            period=period,
            maintenance=maintenance,
            rent=rent,
            lease=lease,
        )

    def _collect_maintenance(
        self, property_id: PropertyId, period: ReportPeriod
    ) -> MaintenanceSectionData:
        try:
            return self._maintenance_source.list_for_period(property_id, period)
        except PermissionError:
            # Authorisation failure: propagate unchanged so the service
            # layer above can handle it (no collection result is returned).
            raise
        except Exception:  # defensive: interface promises not to raise
            # Log the full error internally; surface only a short generic
            # note. One source failing must not kill the other sections.
            logger.exception(
                "maintenance source raised unexpectedly for %s %s",
                property_id,
                period.label,
            )
            return MaintenanceSectionData(
                availability=SectionAvailability.NOT_AVAILABLE,
                note=_MAINTENANCE_SOURCE_NOTE,
            )

    def _collect_lease_rent(
        self, property_id: PropertyId, period: ReportPeriod
    ) -> tuple[RentSectionData, LeaseSectionData]:
        try:
            summary = self._lease_rent_source.period_summary(property_id, period)
            return summary.rent, summary.lease
        except PermissionError:
            # Authorisation failure: propagate unchanged so the service
            # layer above can handle it (no collection result is returned).
            raise
        except Exception:  # defensive: interface promises not to raise
            # Log the full error internally; surface only a short generic
            # note. One source failing must not kill the other sections.
            logger.exception(
                "lease & rent source raised unexpectedly for %s %s",
                property_id,
                period.label,
            )
            note = _LEASE_RENT_SOURCE_NOTE
            rent = RentSectionData(
                availability=SectionAvailability.NOT_AVAILABLE, note=note
            )
            lease = LeaseSectionData(
                availability=SectionAvailability.NOT_AVAILABLE, note=note
            )
            return rent, lease
