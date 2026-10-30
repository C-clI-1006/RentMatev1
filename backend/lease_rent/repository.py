"""SQLite persistence owned by the lease/rent module."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

from contracts.lease_rent import QuestionType

from .fact_query import LeaseRentFactRepository
from .models import (
    LeaseDeadlineAlertCandidate,
    LeaseDeadlineInput,
    LeaseRentSourceScope,
    RentRecordInput,
    VerifiedFactRecord,
)
from .rules import assess_rent


class LeaseRentDataError(ValueError):
    """Raised when stored data is incomplete or structurally invalid."""


class SQLiteLeaseRentRepository(LeaseRentFactRepository):
    """SQLite adapter for verified facts and scheduled UC4/UC5 scans."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def is_authorized(self, *, tenant_id: str, property_id: str) -> bool:
        row = self._connection.execute(
            "SELECT 1 FROM tenant_property "
            "WHERE tenant_id = ? AND property_id = ?",
            (tenant_id, property_id),
        ).fetchone()
        return row is not None

    def resolve_source_scope(
        self, *, source_type: str, source_id: str
    ) -> LeaseRentSourceScope | None:
        """Resolve a source ID to its authoritative tenant/property scope.

        This is the public cross-module lookup for evidence association,
        manager-review scoping and scheduled draft preparation. Callers must
        not accept a property ID from the request and assume that it belongs
        to the supplied record ID.

        A rent row redundantly stores ``property_id`` for efficient monthly
        reporting. The value must agree with its parent lease; disagreement
        is conflicting source data rather than a reason to silently choose
        either value.
        """

        if source_type == "lease":
            rows = self._rows(
                "SELECT lease_id, tenant_id, property_id FROM lease "
                "WHERE lease_id = ?",
                (source_id,),
            )
            if not rows:
                return None
            row = rows[0]
            return LeaseRentSourceScope(
                source_type="lease",
                source_id=str(row["lease_id"]),
                lease_id=str(row["lease_id"]),
                tenant_id=str(row["tenant_id"]),
                property_id=str(row["property_id"]),
            )

        if source_type == "rent_record":
            rows = self._rows(
                "SELECT r.rent_record_id, r.lease_id, r.property_id AS rent_property_id, "
                "l.property_id AS lease_property_id, l.tenant_id "
                "FROM rent_record r JOIN lease l ON l.lease_id = r.lease_id "
                "WHERE r.rent_record_id = ?",
                (source_id,),
            )
            if not rows:
                return None
            row = rows[0]
            if row["rent_property_id"] != row["lease_property_id"]:
                raise LeaseRentDataError(
                    "rent record "
                    f"{source_id} property conflicts with its parent lease"
                )
            return LeaseRentSourceScope(
                source_type="rent_record",
                source_id=str(row["rent_record_id"]),
                lease_id=str(row["lease_id"]),
                tenant_id=str(row["tenant_id"]),
                property_id=str(row["lease_property_id"]),
            )

        raise ValueError("source_type must be 'lease' or 'rent_record'")

    def source_belongs_to_property(
        self, *, source_type: str, source_id: str, property_id: str
    ) -> bool:
        """Return whether a source exists in the requested property scope.

        ``False`` deliberately covers both an unknown record and a record in
        another property, so an unauthorised caller cannot use this predicate
        as a cross-property existence oracle.
        """

        scope = self.resolve_source_scope(
            source_type=source_type,
            source_id=source_id,
        )
        return scope is not None and scope.property_id == property_id

    def reminder_facts(self, *, source_type: str, source_id: str) -> dict:
        """Authoritative, canonical reminder inputs; caller owns the transaction."""
        scope = self.resolve_source_scope(source_type=source_type, source_id=source_id)
        if scope is None:
            raise LeaseRentDataError("reminder source missing")
        lease = self._rows("SELECT start_date,end_date,version,document_ref,created_at FROM lease WHERE lease_id=?", (scope.lease_id,))[0]
        start = _parse_date(lease['start_date'], field_name='start_date')
        end = _parse_date(lease['end_date'], field_name='end_date') if lease['end_date'] is not None else None
        if end is not None and end < start:
            raise LeaseRentDataError("invalid lease dates")
        facts = dict(source_type=source_type, source_id=source_id, lease_id=scope.lease_id,
                     tenant_id=scope.tenant_id, property_id=scope.property_id,
                     start_date=start.isoformat(), end_date=end.isoformat() if end else None,
                     lease_version=lease['version'], document_ref=lease['document_ref'],
                     lease_created_at=lease['created_at'])
        if source_type == 'rent_record':
            row = self._rows("SELECT due_date,amount_due,amount_paid,period,status,created_at FROM rent_record WHERE rent_record_id=?", (source_id,))[0]
            amounts = [Decimal(str(row[k])) for k in ('amount_due', 'amount_paid')]
            if any(not amount.is_finite() or amount < 0 for amount in amounts):
                raise LeaseRentDataError("invalid payment facts")
            facts.update(due_date=_parse_date(row['due_date'], field_name='due_date').isoformat(),
                         amount_due=str(amounts[0].normalize()), amount_paid=str(amounts[1].normalize()),
                         period=row['period'], recorded_status=row['status'], rent_created_at=row['created_at'])
        return facts

    def find_facts(
        self,
        *,
        tenant_id: str,
        property_id: str,
        question_type: QuestionType,
        as_of: datetime,
    ) -> Sequence[VerifiedFactRecord]:
        if question_type is QuestionType.LEASE_DATES:
            return self._lease_date_facts(
                tenant_id=tenant_id, property_id=property_id, as_of=as_of
            )
        if question_type in {QuestionType.RENT_STATUS, QuestionType.RENT_AMOUNT}:
            return self._rent_facts(
                tenant_id=tenant_id,
                property_id=property_id,
                question_type=question_type,
                as_of=as_of,
            )
        # Lease terms require evidence content, not only a document pointer.
        return ()

    def rent_records_for_check(self, *, as_of: date) -> tuple[RentRecordInput, ...]:
        rows = self._rows(
            "SELECT r.rent_record_id, r.due_date, r.amount_due, r.amount_paid "
            "FROM rent_record r JOIN lease l ON l.lease_id = r.lease_id "
            "WHERE date(l.start_date) <= date(?) "
            "AND (l.end_date IS NULL OR date(l.end_date) >= date(?)) "
            "ORDER BY r.rent_record_id",
            (as_of.isoformat(), as_of.isoformat()),
        )
        records: list[RentRecordInput] = []
        for row in rows:
            if any(row[field] is None for field in ("due_date", "amount_due", "amount_paid")):
                raise LeaseRentDataError(
                    f"rent record {row['rent_record_id']} is missing required data"
                )
            records.append(
                RentRecordInput(
                    rent_record_id=row["rent_record_id"],
                    due_date=_parse_date(row["due_date"], field_name="due_date"),
                    amount_due=Decimal(str(row["amount_due"])),
                    amount_paid=Decimal(str(row["amount_paid"])),
                )
            )
        return tuple(records)

    def leases_for_deadline_check(self) -> tuple[LeaseDeadlineInput, ...]:
        rows = self._rows(
            "SELECT lease_id, start_date, end_date FROM lease "
            "WHERE end_date IS NOT NULL ORDER BY lease_id"
        )
        return tuple(
            LeaseDeadlineInput(
                lease_id=row["lease_id"],
                start_date=_parse_date(row["start_date"], field_name="start_date"),
                end_date=_parse_date(row["end_date"], field_name="end_date"),
            )
            for row in rows
        )

    def existing_deadline_alert_keys(self) -> frozenset[str]:
        rows = self._connection.execute(
            "SELECT candidate_key FROM lease_deadline_alert"
        ).fetchall()
        return frozenset(str(row[0]) for row in rows)

    def record_deadline_alert(
        self, candidate: LeaseDeadlineAlertCandidate, *, created_at: datetime
    ) -> bool:
        lease = self._connection.execute(
            "SELECT tenant_id, property_id FROM lease WHERE lease_id = ?",
            (candidate.lease_id,),
        ).fetchone()
        if lease is None:
            raise LeaseRentDataError(f"lease {candidate.lease_id} does not exist")

        with self._connection:
            cursor = self._connection.execute(
                "INSERT OR IGNORE INTO lease_deadline_alert "
                "(alert_id, candidate_key, lease_id, tenant_id, property_id, "
                "end_date, remaining_days, prepared_on, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(uuid4()),
                    candidate.candidate_key,
                    candidate.lease_id,
                    lease[0],
                    lease[1],
                    candidate.end_date.isoformat(),
                    candidate.remaining_days,
                    candidate.prepared_on.isoformat(),
                    _utc_text(created_at),
                ),
            )
        return cursor.rowcount == 1

    def _lease_date_facts(
        self, *, tenant_id: str, property_id: str, as_of: datetime
    ) -> tuple[VerifiedFactRecord, ...]:
        day = as_of.date().isoformat()
        rows = self._rows(
            "SELECT lease_id, start_date, end_date, version, created_at "
            "FROM lease WHERE tenant_id = ? AND property_id = ? "
            "AND date(start_date) <= date(?) "
            "AND (end_date IS NULL OR date(end_date) >= date(?))",
            (tenant_id, property_id, day, day),
        )
        facts: list[VerifiedFactRecord] = []
        for row in rows:
            observed_at = _parse_datetime(row["created_at"])
            common = {
                "observed_at": observed_at,
                "source_type": "system",
                "source_id": row["lease_id"],
                "lease_id": row["lease_id"],
                "version": row["version"],
            }
            facts.append(
                VerifiedFactRecord(name="lease_start_date", value=row["start_date"], **common)
            )
            if row["end_date"] is not None:
                facts.append(
                    VerifiedFactRecord(
                        name="lease_end_date", value=row["end_date"], **common
                    )
                )
        return tuple(facts)

    def _rent_facts(
        self,
        *,
        tenant_id: str,
        property_id: str,
        question_type: QuestionType,
        as_of: datetime,
    ) -> tuple[VerifiedFactRecord, ...]:
        day = as_of.date().isoformat()
        rows = self._rows(
            "SELECT r.rent_record_id, r.due_date, r.amount_due, r.amount_paid, "
            "r.created_at, l.lease_id FROM rent_record r "
            "JOIN lease l ON l.lease_id = r.lease_id "
            "WHERE l.tenant_id = ? AND l.property_id = ? AND r.property_id = ? "
            "AND r.period = ? AND date(l.start_date) <= date(?) "
            "AND (l.end_date IS NULL OR date(l.end_date) >= date(?))",
            (
                tenant_id,
                property_id,
                property_id,
                as_of.strftime("%Y-%m"),
                day,
                day,
            ),
        )
        facts: list[VerifiedFactRecord] = []
        for row in rows:
            if any(row[field] is None for field in ("due_date", "amount_due", "amount_paid")):
                continue
            record = RentRecordInput(
                rent_record_id=row["rent_record_id"],
                due_date=_parse_date(row["due_date"], field_name="due_date"),
                amount_due=Decimal(str(row["amount_due"])),
                amount_paid=Decimal(str(row["amount_paid"])),
            )
            assessment = assess_rent(record, as_of=as_of.date())
            observed_at = _parse_datetime(row["created_at"])
            common = {
                "observed_at": observed_at,
                "source_type": "rent_record",
                "source_id": record.rent_record_id,
                "lease_id": row["lease_id"],
                "version": row["created_at"],
                "stale_after": timedelta(days=1),
            }
            if question_type is QuestionType.RENT_STATUS:
                facts.extend(
                    [
                        VerifiedFactRecord(
                            name="current_period_status",
                            value=assessment.status.value,
                            **common,
                        ),
                        VerifiedFactRecord(
                            name="rent_due_date",
                            value=record.due_date.isoformat(),
                            **common,
                        ),
                        VerifiedFactRecord(
                            name="overdue_days",
                            value=str(assessment.overdue_days),
                            **common,
                        ),
                    ]
                )
            else:
                facts.extend(
                    [
                        VerifiedFactRecord(
                            name="amount_due",
                            value=_decimal_text(record.amount_due),
                            **common,
                        ),
                        VerifiedFactRecord(
                            name="amount_paid",
                            value=_decimal_text(record.amount_paid),
                            **common,
                        ),
                        VerifiedFactRecord(
                            name="outstanding_amount",
                            value=_decimal_text(assessment.outstanding_amount),
                            **common,
                        ),
                    ]
                )
        return tuple(facts)

    def _rows(self, sql: str, parameters: tuple = ()) -> list[dict]:
        cursor = self._connection.execute(sql, parameters)
        names = [column[0] for column in cursor.description]
        return [dict(zip(names, tuple(row))) for row in cursor.fetchall()]


def _parse_date(value: str, *, field_name: str) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as error:
        raise LeaseRentDataError(f"invalid {field_name}: {value!r}") from error


def _parse_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as error:
        raise LeaseRentDataError(f"invalid UTC timestamp: {value!r}") from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _utc_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("created_at must be timezone-aware UTC")
    return value.isoformat().replace("+00:00", "Z")


def _decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"
