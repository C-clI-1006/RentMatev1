"""Lease/Rent-owned SQLite adapter for monthly reporting."""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from decimal import Decimal

from contracts.common import PropertyId
from contracts.reporting import (
    ILeaseRentReportSource,
    LeaseRef,
    LeaseRentPeriodData,
    LeaseSectionData,
    RentRecordRef,
    RentSectionData,
    ReportPeriod,
    SectionAvailability,
    SourceReference,
)


class SQLiteLeaseRentReportSource(ILeaseRentReportSource):
    """Read the lease/rent module's verified tables for one property/month."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def period_summary(
        self, property_id: PropertyId, period: ReportPeriod
    ) -> LeaseRentPeriodData:
        try:
            rent_rows = self._connection.execute(
                "SELECT rent_record_id,period,amount_due,amount_paid,status,created_at "
                "FROM rent_record WHERE property_id=? AND period=? "
                "ORDER BY rent_record_id",
                (str(property_id), period.label),
            ).fetchall()
            lease_rows = self._connection.execute(
                "SELECT lease_id,tenant_id,start_date,end_date,version,created_at "
                "FROM lease WHERE property_id=? AND date(start_date)<date(?) "
                "AND (end_date IS NULL OR date(end_date)>=date(?)) ORDER BY lease_id",
                (str(property_id), period.end.date().isoformat(), period.start.date().isoformat()),
            ).fetchall()
        except sqlite3.Error:
            note = "lease & rent data could not be read"
            return LeaseRentPeriodData(
                rent=RentSectionData(
                    availability=SectionAvailability.NOT_AVAILABLE, note=note
                ),
                lease=LeaseSectionData(
                    availability=SectionAvailability.NOT_AVAILABLE, note=note
                ),
            )

        try:
            rent = [
                RentRecordRef(
                    rent_record_id=row["rent_record_id"],
                    period=row["period"],
                    amount_due=(
                        Decimal(str(row["amount_due"]))
                        if row["amount_due"] is not None
                        else None
                    ),
                    amount_paid=(
                        Decimal(str(row["amount_paid"]))
                        if row["amount_paid"] is not None
                        else None
                    ),
                    status=row["status"],
                    references=[
                        SourceReference(
                            source_type="rent_record",
                            source_id=row["rent_record_id"],
                            version=row["created_at"],
                        )
                    ],
                )
                for row in rent_rows
            ]
            leases = [self._lease_ref(row, period) for row in lease_rows]
        except (ValueError, TypeError, KeyError):
            note = "lease & rent data contains an invalid record"
            return LeaseRentPeriodData(
                rent=RentSectionData(
                    availability=SectionAvailability.NOT_AVAILABLE, note=note
                ),
                lease=LeaseSectionData(
                    availability=SectionAvailability.NOT_AVAILABLE, note=note
                ),
            )
        return LeaseRentPeriodData(
            rent=RentSectionData(
                availability=(
                    SectionAvailability.AVAILABLE if rent else SectionAvailability.EMPTY
                ),
                records=rent,
            ),
            lease=LeaseSectionData(
                availability=(
                    SectionAvailability.AVAILABLE if leases else SectionAvailability.EMPTY
                ),
                leases=leases,
            ),
        )

    @staticmethod
    def _lease_ref(row: sqlite3.Row, period: ReportPeriod) -> LeaseRef:
        start = date.fromisoformat(row["start_date"])
        end = date.fromisoformat(row["end_date"]) if row["end_date"] else None
        at_end = period.end.date()
        if end is not None and end < at_end:
            status = "expired"
        elif end is not None and end <= at_end + timedelta(days=30):
            status = "expiring"
        elif start >= at_end:
            status = "upcoming"
        else:
            status = "active"
        return LeaseRef(
            lease_id=row["lease_id"],
            tenant_id=row["tenant_id"],
            start_date=start,
            end_date=end,
            status=status,
            references=[
                SourceReference(
                    source_type="lease",
                    source_id=row["lease_id"],
                    version=row["version"] or row["created_at"],
                )
            ],
        )
