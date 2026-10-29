"""Reporting adapter for verified monthly lease and rent data.

This module implements the cross-module ``ILeaseRentReportSource`` contract.
It keeps reporting read-only, calculates statuses from structured records, and
preserves the important distinction between an empty period and an unavailable
data source.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

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
    REPORTING_TIMEZONE,
)

from .models import RentRecordInput
from .repository import LeaseRentDataError
from .rules import assess_rent

logger = logging.getLogger(__name__)

_RENT_UNAVAILABLE_NOTE = "rent data is temporarily unavailable"
_LEASE_UNAVAILABLE_NOTE = "lease data is temporarily unavailable"


def _business_today() -> date:
    return datetime.now(ZoneInfo(REPORTING_TIMEZONE)).date()


class SQLiteLeaseRentReportSource(ILeaseRentReportSource):
    """Read monthly reporting data from the lease/rent SQLite store.

    Property authorisation is intentionally handled by the reporting service
    before this provider is called, as defined by the shared reporting
    contract. The provider owns only the data selection, deterministic status
    calculation, and source provenance.
    """

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        reminder_window_days: int = 30,
        today: Callable[[], date] = _business_today,
    ) -> None:
        if reminder_window_days < 0:
            raise ValueError("reminder_window_days cannot be negative")
        self._connection = connection
        self._reminder_window_days = reminder_window_days
        self._today = today

    def period_summary(
        self, property_id: PropertyId, period: ReportPeriod
    ) -> LeaseRentPeriodData:
        """Return independent rent and lease sections for one report month."""

        return LeaseRentPeriodData(
            rent=self._rent_section(str(property_id), period),
            lease=self._lease_section(str(property_id), period),
        )

    def _rent_section(
        self, property_id: str, period: ReportPeriod
    ) -> RentSectionData:
        try:
            rows = self._rows(
                "SELECT rent_record_id, period, due_date, amount_due, "
                "amount_paid, created_at FROM rent_record "
                "WHERE property_id = ? AND period = ? "
                "ORDER BY rent_record_id",
                (property_id, period.label),
            )
            if not rows:
                return RentSectionData(availability=SectionAvailability.EMPTY)

            records: list[RentRecordRef] = []
            as_of = self._today()
            for row in rows:
                required = ("due_date", "amount_due", "amount_paid")
                if any(row[field] is None for field in required):
                    raise LeaseRentDataError(
                        "a rent record is missing required reporting data"
                    )
                record = RentRecordInput(
                    rent_record_id=str(row["rent_record_id"]),
                    due_date=_parse_date(row["due_date"], field_name="due_date"),
                    amount_due=Decimal(str(row["amount_due"])),
                    amount_paid=Decimal(str(row["amount_paid"])),
                )
                assessment = assess_rent(record, as_of=as_of)
                records.append(
                    RentRecordRef(
                        rent_record_id=record.rent_record_id,
                        period=str(row["period"]),
                        amount_due=record.amount_due,
                        amount_paid=record.amount_paid,
                        status=assessment.status.value,
                        references=[
                            SourceReference(
                                source_type="rent_record",
                                source_id=record.rent_record_id,
                                version=_optional_text(row["created_at"]),
                            )
                        ],
                    )
                )
            return RentSectionData(
                availability=SectionAvailability.AVAILABLE,
                records=records,
            )
        except PermissionError:
            raise
        except (sqlite3.Error, LeaseRentDataError, ValueError, InvalidOperation):
            logger.exception(
                "rent reporting query failed for %s %s", property_id, period.label
            )
            return RentSectionData(
                availability=SectionAvailability.NOT_AVAILABLE,
                note=_RENT_UNAVAILABLE_NOTE,
            )

    def _lease_section(
        self, property_id: str, period: ReportPeriod
    ) -> LeaseSectionData:
        try:
            rows = self._rows(
                "SELECT lease_id, tenant_id, start_date, end_date, version, "
                "created_at FROM lease WHERE property_id = ? "
                "AND date(start_date) < date(?) "
                "AND (end_date IS NULL OR date(end_date) >= date(?)) "
                "ORDER BY lease_id",
                (
                    property_id,
                    period.end_date.isoformat(),
                    period.start_date.isoformat(),
                ),
            )
            if not rows:
                return LeaseSectionData(availability=SectionAvailability.EMPTY)

            leases: list[LeaseRef] = []
            as_of = self._today()
            for row in rows:
                start_date = _parse_date(row["start_date"], field_name="start_date")
                end_date = (
                    _parse_date(row["end_date"], field_name="end_date")
                    if row["end_date"] is not None
                    else None
                )
                if end_date is not None and end_date < start_date:
                    raise LeaseRentDataError(
                        "a lease end date is before its start date"
                    )
                version = _optional_text(row["version"]) or _optional_text(
                    row["created_at"]
                )
                leases.append(
                    LeaseRef(
                        lease_id=str(row["lease_id"]),
                        tenant_id=str(row["tenant_id"]),
                        start_date=start_date,
                        end_date=end_date,
                        status=_lease_status(
                            start_date=start_date,
                            end_date=end_date,
                            as_of=as_of,
                            reminder_window_days=self._reminder_window_days,
                        ),
                        references=[
                            SourceReference(
                                source_type="lease",
                                source_id=str(row["lease_id"]),
                                version=version,
                            )
                        ],
                    )
                )
            return LeaseSectionData(
                availability=SectionAvailability.AVAILABLE,
                leases=leases,
            )
        except PermissionError:
            raise
        except (sqlite3.Error, LeaseRentDataError, ValueError):
            logger.exception(
                "lease reporting query failed for %s %s", property_id, period.label
            )
            return LeaseSectionData(
                availability=SectionAvailability.NOT_AVAILABLE,
                note=_LEASE_UNAVAILABLE_NOTE,
            )

    def _rows(self, sql: str, parameters: tuple = ()) -> list[dict]:
        cursor = self._connection.execute(sql, parameters)
        names = [column[0] for column in cursor.description]
        return [dict(zip(names, tuple(row))) for row in cursor.fetchall()]


def _lease_status(
    *,
    start_date: date,
    end_date: date | None,
    as_of: date,
    reminder_window_days: int,
) -> str:
    """Derive the report status from dates without trusting free text."""

    if as_of < start_date:
        return "upcoming"
    if end_date is not None and as_of > end_date:
        return "expired"
    if end_date is not None and (end_date - as_of).days <= reminder_window_days:
        return "expiring"
    return "active"


def _parse_date(value: object, *, field_name: str) -> date:
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError) as error:
        raise LeaseRentDataError(f"invalid {field_name}") from error


def _optional_text(value: object) -> str | None:
    return None if value is None else str(value)
