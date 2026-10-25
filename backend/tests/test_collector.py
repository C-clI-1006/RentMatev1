"""Tests for reporting.collector with explicitly marked fake providers.

Scenarios covered (per this round's task):
- COMPLETE:  all three sections AVAILABLE with data;
- EMPTY:     source reachable, zero in-scope records (stays EMPTY,
             never collapsed into AVAILABLE or NOT_AVAILABLE);
- PARTIAL:   one source unavailable (NOT_AVAILABLE + note) while the
             other sections keep their own states.

Run: pytest backend/tests/test_collector.py
"""

from datetime import date, datetime, timezone

import pytest

from contracts.common import PropertyId
from contracts.maintenance import MaintenanceStatus, Priority
from contracts.reporting import (
    ILeaseRentReportSource,
    IMaintenanceReportSource,
    LeaseRentPeriodData,
    LeaseRef,
    LeaseSectionData,
    MaintenanceRequestRef,
    MaintenanceSectionData,
    RentRecordRef,
    RentSectionData,
    ReportPeriod,
    SectionAvailability,
    SourceReference,
)
from reporting.collector import (
    CollectedReportData,
    ReportDataCollector,
    _LEASE_RENT_SOURCE_NOTE,
    _MAINTENANCE_SOURCE_NOTE,
)

# ---------------------------------------------------------------------------
# Explicitly marked fake providers (test doubles, NOT real implementations)
# ---------------------------------------------------------------------------

FAKE_PERIOD = ReportPeriod(year=2026, month=10)
FAKE_PROPERTY = PropertyId("prop-1")


class FakeMaintenanceSource(IMaintenanceReportSource):
    """FAKE provider: serves a canned MaintenanceSectionData, or raises."""

    def __init__(self, section: MaintenanceSectionData | Exception):
        self._section = section
        self.calls: list[tuple[str, ReportPeriod]] = []

    def list_for_period(self, property_id, period) -> MaintenanceSectionData:
        self.calls.append((property_id, period))
        if isinstance(self._section, Exception):
            raise self._section
        return self._section


class FakeLeaseRentSource(ILeaseRentReportSource):
    """FAKE provider: serves a canned LeaseRentPeriodData, or raises."""

    def __init__(self, summary: LeaseRentPeriodData | Exception):
        self._summary = summary
        self.calls: list[tuple[str, ReportPeriod]] = []

    def period_summary(self, property_id, period) -> LeaseRentPeriodData:
        self.calls.append((property_id, period))
        if isinstance(self._summary, Exception):
            raise self._summary
        return self._summary


# ---------------------------------------------------------------------------
# Fixtures (fake data, timezone-aware UTC)
# ---------------------------------------------------------------------------

T1 = datetime(2026, 10, 3, 9, 0, tzinfo=timezone.utc)
T2 = datetime(2026, 10, 4, 8, 0, tzinfo=timezone.utc)


def _maintenance_available() -> MaintenanceSectionData:
    return MaintenanceSectionData(
        availability=SectionAvailability.AVAILABLE,
        requests=[
            MaintenanceRequestRef(
                request_id="req-1",
                title="No hot water",
                status=MaintenanceStatus.IN_PROGRESS,
                priority=Priority.URGENT,
                created_at=T1,
                last_activity_at=T2,
                references=[
                    SourceReference(
                        source_type="maintenance_request", source_id="req-1", version="1"
                    )
                ],
            )
        ],
    )


def _rent_available() -> RentSectionData:
    return RentSectionData(
        availability=SectionAvailability.AVAILABLE,
        records=[
            RentRecordRef(
                rent_record_id="rr-1",
                period="2026-10",
                amount_due="1200.00",
                amount_paid=None,
                status="unpaid-but-not-due",
                references=[SourceReference(source_type="rent_record", source_id="rr-1")],
            )
        ],
    )


def _lease_available() -> LeaseSectionData:
    return LeaseSectionData(
        availability=SectionAvailability.AVAILABLE,
        leases=[
            LeaseRef(
                lease_id="lease-1",
                tenant_id="t-1",
                start_date=date(2026, 1, 1),
                end_date=date(2026, 12, 31),
                status="active",
                references=[SourceReference(source_type="lease", source_id="lease-1")],
            )
        ],
    )


def _empty(section_cls, **kwargs):
    return section_cls(availability=SectionAvailability.EMPTY, **kwargs)


# ---------------------------------------------------------------------------
# COMPLETE
# ---------------------------------------------------------------------------


def test_complete_all_sections_available():
    maint_src = FakeMaintenanceSource(_maintenance_available())
    lease_rent_src = FakeLeaseRentSource(
        LeaseRentPeriodData(rent=_rent_available(), lease=_lease_available())
    )
    collector = ReportDataCollector(maint_src, lease_rent_src)

    data = collector.collect(FAKE_PROPERTY, FAKE_PERIOD)

    assert isinstance(data, CollectedReportData)
    assert data.property_id == "prop-1"
    assert data.period.label == "2026-10"

    # Every section keeps its AVAILABLE state and its data.
    assert data.maintenance.availability == SectionAvailability.AVAILABLE
    assert [r.request_id for r in data.maintenance.requests] == ["req-1"]
    assert data.maintenance.requests[0].status == MaintenanceStatus.IN_PROGRESS
    assert data.rent.availability == SectionAvailability.AVAILABLE
    assert [r.rent_record_id for r in data.rent.records] == ["rr-1"]
    assert data.lease.availability == SectionAvailability.AVAILABLE
    assert [l.lease_id for l in data.lease.leases] == ["lease-1"]

    # Provenance survives collection (NFR-PR-01).
    assert data.maintenance.requests[0].references[0].source_type == "maintenance_request"
    assert data.rent.records[0].references[0].source_type == "rent_record"
    assert data.lease.leases[0].references[0].source_type == "lease"

    # No report-level outcome / narrative is produced by the collector.
    assert "outcome" not in CollectedReportData.model_fields
    assert "narrative" not in CollectedReportData.model_fields
    assert data.collected_at.tzinfo is not None

    # Each source consulted exactly once, with the requested scope.
    assert maint_src.calls == [(FAKE_PROPERTY, FAKE_PERIOD)]
    assert lease_rent_src.calls == [(FAKE_PROPERTY, FAKE_PERIOD)]


# ---------------------------------------------------------------------------
# EMPTY (reachable, zero in-scope records)
# ---------------------------------------------------------------------------


def test_empty_stays_empty_not_available():
    maint_src = FakeMaintenanceSource(_empty(MaintenanceSectionData))
    lease_rent_src = FakeLeaseRentSource(
        LeaseRentPeriodData(
            rent=_empty(RentSectionData),
            lease=_empty(LeaseSectionData),
        )
    )
    collector = ReportDataCollector(maint_src, lease_rent_src)

    data = collector.collect(FAKE_PROPERTY, FAKE_PERIOD)

    # EMPTY is a DISTINCT state: preserved as EMPTY, never collapsed.
    assert data.maintenance.availability == SectionAvailability.EMPTY
    assert data.maintenance.requests == []
    assert data.rent.availability == SectionAvailability.EMPTY
    assert data.rent.records == []
    assert data.lease.availability == SectionAvailability.EMPTY
    assert data.lease.leases == []


# ---------------------------------------------------------------------------
# PARTIAL UNAVAILABLE
# ---------------------------------------------------------------------------


def test_partial_unavailable_kept_per_section():
    maint_src = FakeMaintenanceSource(_maintenance_available())
    rent_unavailable = RentSectionData(
        availability=SectionAvailability.NOT_AVAILABLE,
        note="rent service returned 503 during the collection window",
    )
    lease_rent_src = FakeLeaseRentSource(
        LeaseRentPeriodData(rent=rent_unavailable, lease=_lease_available())
    )
    collector = ReportDataCollector(maint_src, lease_rent_src)

    data = collector.collect(FAKE_PROPERTY, FAKE_PERIOD)

    # The unavailable section keeps its state AND its human-readable note
    # (never rendered as "no problem" / "no records").
    assert data.rent.availability == SectionAvailability.NOT_AVAILABLE
    assert data.rent.note == "rent service returned 503 during the collection window"
    assert data.rent.records == []

    # Other sections from the same source are unaffected.
    assert data.lease.availability == SectionAvailability.AVAILABLE
    assert [l.lease_id for l in data.lease.leases] == ["lease-1"]
    assert data.maintenance.availability == SectionAvailability.AVAILABLE
    assert len(data.maintenance.requests) == 1


def test_raising_source_is_marked_not_available_with_note():
    """Defence: if a source violates its 'must not raise' promise, the
    collector still encodes the failure as NOT_AVAILABLE + a short GENERIC
    note (exception text never leaks into the externally rendered note)
    for the affected sections, and the other source's sections are
    unaffected."""
    maint_src = FakeMaintenanceSource(RuntimeError("db down"))
    lease_rent_src = FakeLeaseRentSource(
        LeaseRentPeriodData(rent=_rent_available(), lease=_lease_available())
    )
    collector = ReportDataCollector(maint_src, lease_rent_src)

    data = collector.collect(FAKE_PROPERTY, FAKE_PERIOD)

    assert data.maintenance.availability == SectionAvailability.NOT_AVAILABLE
    assert data.maintenance.note == _MAINTENANCE_SOURCE_NOTE
    assert "db down" not in data.maintenance.note  # no exception text leaked
    assert data.maintenance.requests == []
    # The other source's sections are preserved.
    assert data.rent.availability == SectionAvailability.AVAILABLE
    assert data.lease.availability == SectionAvailability.AVAILABLE


def test_raising_lease_rent_source_does_not_kill_maintenance():
    """A lease & rent source failure must not kill the maintenance section."""
    maint_src = FakeMaintenanceSource(_maintenance_available())
    lease_rent_src = FakeLeaseRentSource(ConnectionError("lease_rent offline"))
    collector = ReportDataCollector(maint_src, lease_rent_src)

    data = collector.collect(FAKE_PROPERTY, FAKE_PERIOD)

    assert data.maintenance.availability == SectionAvailability.AVAILABLE
    assert len(data.maintenance.requests) == 1
    assert data.rent.availability == SectionAvailability.NOT_AVAILABLE
    assert data.rent.note == _LEASE_RENT_SOURCE_NOTE
    assert "lease_rent offline" not in data.rent.note  # no exception text leaked
    assert data.lease.availability == SectionAvailability.NOT_AVAILABLE
    assert data.lease.note == _LEASE_RENT_SOURCE_NOTE


def test_maintenance_permission_denied_propagates_without_result():
    """PermissionError from the maintenance source is an authorisation
    failure, not a data gap: the collector must NOT degrade it into
    NOT_AVAILABLE — it propagates unchanged, and no collection result is
    returned to the caller."""
    maint_src = FakeMaintenanceSource(PermissionError("access denied"))
    lease_rent_src = FakeLeaseRentSource(
        LeaseRentPeriodData(rent=_rent_available(), lease=_lease_available())
    )
    collector = ReportDataCollector(maint_src, lease_rent_src)

    with pytest.raises(PermissionError):
        collector.collect(FAKE_PROPERTY, FAKE_PERIOD)

    # Collection aborted before consulting the second source: nothing
    # collected, no partial result returned.
    assert maint_src.calls == [(FAKE_PROPERTY, FAKE_PERIOD)]
    assert lease_rent_src.calls == []


def test_lease_rent_permission_denied_propagates_without_result():
    """PermissionError from the lease & rent source propagates unchanged
    (no NOT_AVAILABLE degradation, no collection result returned), even
    though the maintenance section was already read."""
    maint_src = FakeMaintenanceSource(_maintenance_available())
    lease_rent_src = FakeLeaseRentSource(PermissionError("access denied"))
    collector = ReportDataCollector(maint_src, lease_rent_src)

    with pytest.raises(PermissionError):
        collector.collect(FAKE_PROPERTY, FAKE_PERIOD)

    # The first source was consulted, but the failure still means no
    # collection result is returned at all.
    assert maint_src.calls == [(FAKE_PROPERTY, FAKE_PERIOD)]
    assert lease_rent_src.calls == [(FAKE_PROPERTY, FAKE_PERIOD)]


# ---------------------------------------------------------------------------
# Boundary: collector must not mutate or re-derive section data
# ---------------------------------------------------------------------------


def test_collector_passes_source_sections_through_unchanged():
    maint = _maintenance_available()
    rent = _rent_available()
    lease = _lease_available()
    maint_src = FakeMaintenanceSource(maint)
    lease_rent_src = FakeLeaseRentSource(LeaseRentPeriodData(rent=rent, lease=lease))
    collector = ReportDataCollector(maint_src, lease_rent_src)

    data = collector.collect(FAKE_PROPERTY, FAKE_PERIOD)

    assert data.maintenance == maint
    assert data.rent == rent
    assert data.lease == lease
