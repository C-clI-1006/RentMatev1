"""Integration tests for the lease/rent monthly reporting provider."""

import sqlite3
from datetime import date
from pathlib import Path

import pytest

from contracts.common import PropertyId
from contracts.reporting import (
    MaintenanceSectionData,
    ReportPeriod,
    SectionAvailability,
)
from lease_rent import SQLiteLeaseRentReportSource
from reporting.collector import ReportDataCollector


SCHEMA_PATH = Path(__file__).resolve().parents[2] / "db" / "schema.sql"
PERIOD = ReportPeriod(year=2026, month=10)
TODAY = date(2026, 10, 20)


@pytest.fixture()
def db() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    connection.executescript(
        """
        INSERT INTO tenant (tenant_id, name, created_at)
        VALUES ('tenant-1', 'Tenant', '2026-01-01T00:00:00Z');
        INSERT INTO property (property_id, name, created_at)
        VALUES ('property-1', 'Home', '2026-01-01T00:00:00Z');
        INSERT INTO tenant_property (tenant_id, property_id, created_at)
        VALUES ('tenant-1', 'property-1', '2026-01-01T00:00:00Z');
        INSERT INTO lease (
            lease_id, property_id, tenant_id, start_date, end_date, version, created_at
        ) VALUES (
            'lease-1', 'property-1', 'tenant-1', '2026-01-01', '2026-11-01',
            '2', '2026-01-01T00:00:00Z'
        );
        INSERT INTO rent_record (
            rent_record_id, lease_id, property_id, period, due_date,
            amount_due, amount_paid, status, created_at
        ) VALUES (
            'rent-1', 'lease-1', 'property-1', '2026-10', '2026-10-10',
            1000, 250, 'unpaid-but-not-due', '2026-10-20T08:00:00Z'
        );
        """
    )
    yield connection
    connection.close()


def _source(db: sqlite3.Connection) -> SQLiteLeaseRentReportSource:
    return SQLiteLeaseRentReportSource(db, today=lambda: TODAY)


def test_period_summary_returns_traceable_rent_and_lease_data(db):
    summary = _source(db).period_summary(PropertyId("property-1"), PERIOD)

    assert summary.rent.availability is SectionAvailability.AVAILABLE
    assert len(summary.rent.records) == 1
    rent = summary.rent.records[0]
    assert rent.amount_due == 1000
    assert rent.amount_paid == 250
    # Stored status is deliberately stale: the provider must calculate it.
    assert rent.status == "overdue"
    assert rent.references[0].source_id == "rent-1"

    assert summary.lease.availability is SectionAvailability.AVAILABLE
    assert len(summary.lease.leases) == 1
    lease = summary.lease.leases[0]
    assert lease.status == "expiring"
    assert lease.references[0].source_id == "lease-1"
    assert lease.references[0].version == "2"


def test_provider_plugs_into_reporting_collector(db):
    class EmptyMaintenanceSource:
        def list_for_period(self, property_id, period):
            return MaintenanceSectionData(availability=SectionAvailability.EMPTY)

    collector = ReportDataCollector(EmptyMaintenanceSource(), _source(db))

    collected = collector.collect(PropertyId("property-1"), PERIOD)

    assert collected.maintenance.availability is SectionAvailability.EMPTY
    assert collected.rent.records[0].status == "overdue"
    assert collected.lease.leases[0].status == "expiring"


def test_reachable_source_with_no_records_is_empty(db):
    summary = _source(db).period_summary(PropertyId("property-with-no-data"), PERIOD)

    assert summary.rent.availability is SectionAvailability.EMPTY
    assert summary.rent.records == []
    assert summary.lease.availability is SectionAvailability.EMPTY
    assert summary.lease.leases == []


def test_lease_date_bounds_do_not_shift_to_previous_utc_day(db):
    db.executescript("""
        INSERT INTO lease (lease_id,property_id,tenant_id,start_date,end_date,created_at)
        VALUES ('ends-september','property-1','tenant-1','2026-01-01','2026-09-30','2026-01-01'),
               ('starts-last-october-day','property-1','tenant-1','2026-10-31','2027-01-01','2026-01-01'),
               ('starts-november','property-1','tenant-1','2026-11-01','2027-01-01','2026-01-01');
    """)
    leases = _source(db).period_summary(PropertyId("property-1"), PERIOD).lease.leases
    ids = {lease.lease_id for lease in leases}
    assert "starts-last-october-day" in ids
    assert "ends-september" not in ids
    assert "starts-november" not in ids


def test_partial_payment_never_creates_a_partial_status(db):
    db.execute(
        "UPDATE rent_record SET due_date = '2026-10-25' "
        "WHERE rent_record_id = 'rent-1'"
    )

    summary = _source(db).period_summary(PropertyId("property-1"), PERIOD)

    assert summary.rent.records[0].status == "unpaid-but-not-due"
    assert summary.rent.records[0].status != "partial"


def test_lease_statuses_are_derived_from_dates(db):
    db.executescript(
        """
        INSERT INTO lease (
            lease_id, property_id, tenant_id, start_date, end_date, version, created_at
        ) VALUES
            ('active', 'property-1', 'tenant-1', '2026-01-01', '2027-01-01',
             '1', '2026-01-01T00:00:00Z'),
            ('expired', 'property-1', 'tenant-1', '2026-01-01', '2026-10-10',
             '1', '2026-01-01T00:00:00Z'),
            ('upcoming', 'property-1', 'tenant-1', '2026-10-25', '2027-01-25',
             '1', '2026-01-01T00:00:00Z');
        """
    )

    summary = _source(db).period_summary(PropertyId("property-1"), PERIOD)
    statuses = {lease.lease_id: lease.status for lease in summary.lease.leases}

    assert statuses == {
        "active": "active",
        "expired": "expired",
        "lease-1": "expiring",
        "upcoming": "upcoming",
    }


def test_invalid_rent_data_does_not_hide_available_lease_data(db):
    db.execute(
        "UPDATE rent_record SET due_date = 'not-a-date' "
        "WHERE rent_record_id = 'rent-1'"
    )

    summary = _source(db).period_summary(PropertyId("property-1"), PERIOD)

    assert summary.rent.availability is SectionAvailability.NOT_AVAILABLE
    assert summary.rent.note == "rent data is temporarily unavailable"
    assert summary.rent.records == []
    assert summary.lease.availability is SectionAvailability.AVAILABLE
    assert len(summary.lease.leases) == 1


def test_unreachable_database_is_not_reported_as_empty(db):
    source = _source(db)
    db.close()

    summary = source.period_summary(PropertyId("property-1"), PERIOD)

    assert summary.rent.availability is SectionAvailability.NOT_AVAILABLE
    assert summary.rent.note
    assert summary.lease.availability is SectionAvailability.NOT_AVAILABLE
    assert summary.lease.note
