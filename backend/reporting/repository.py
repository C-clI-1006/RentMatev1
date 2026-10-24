"""Reporting-owned SQLite persistence for generated monthly snapshots."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from contracts.reporting import (
    LeaseSectionData,
    MaintenanceSectionData,
    ManagerReportTrigger,
    OpenTasksSectionData,
    ReportNarrative,
    ReportOutcome,
    ReportPeriod,
    ReportRecord,
    RisksSectionData,
    RentSectionData,
    ScheduledReportTrigger,
)
from shared import new_id


class ReportRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def get(self, report_id: str, property_id: str) -> ReportRecord | None:
        """Read one report only within the requested property."""
        row = self.connection.execute(
            "SELECT * FROM report WHERE report_id=? AND property_id=?",
            (str(report_id), str(property_id)),
        ).fetchone()
        return self._record(row) if row is not None else None

    def list_for_period(self, property_id: str, period: ReportPeriod) -> list[ReportRecord]:
        rows = self.connection.execute(
            "SELECT * FROM report WHERE property_id=? AND period_year=? "
            "AND period_month=? ORDER BY generation",
            (str(property_id), period.year, period.month),
        ).fetchall()
        return [self._record(row) for row in rows]

    def property_exists(self, property_id: str) -> bool:
        return (
            self.connection.execute(
                "SELECT 1 FROM property WHERE property_id=?", (property_id,)
            ).fetchone()
            is not None
        )

    def by_event(self, event_id: str) -> ReportRecord | None:
        row = self.connection.execute(
            "SELECT * FROM report WHERE event_id=?", (event_id,)
        ).fetchone()
        return self._record(row) if row else None

    def next_generation(self, property_id: str, period: ReportPeriod) -> int:
        row = self.connection.execute(
            "SELECT COALESCE(MAX(generation),0)+1 FROM report "
            "WHERE property_id=? AND period_year=? AND period_month=?",
            (property_id, period.year, period.month),
        ).fetchone()
        return int(row[0])

    def insert(self, record: ReportRecord) -> ReportRecord:
        data = {
            "trigger": record.trigger.model_dump(mode="json"),
            "maintenance": record.maintenance.model_dump(mode="json"),
            "rent": record.rent.model_dump(mode="json"),
            "lease": record.lease.model_dump(mode="json"),
            "open_tasks": record.open_tasks.model_dump(mode="json"),
            "risks": record.risks.model_dump(mode="json"),
        }
        manager_id = (
            record.trigger.manager_id
            if isinstance(record.trigger, ManagerReportTrigger)
            else None
        )
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            existing = self.by_event(str(record.event_id))
            if existing is not None:
                self.connection.rollback()
                return existing
            generation = self.next_generation(str(record.property_id), record.period)
            record.generation = generation
            self.connection.execute(
                "INSERT INTO report "
                "(report_id,property_id,period_year,period_month,generation,trigger_kind,"
                "event_id,manager_id,outcome,data_json,narrative_json,data_as_of,created_at,"
                "created_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    str(record.report_id),
                    str(record.property_id),
                    record.period.year,
                    record.period.month,
                    generation,
                    record.trigger.trigger,
                    str(record.event_id),
                    manager_id,
                    record.outcome.value,
                    json.dumps(data, ensure_ascii=False),
                    record.narrative.model_dump_json(),
                    _iso(record.data_as_of),
                    _iso(record.created_at),
                    record.created_by,
                ),
            )
            action = "report.failed" if record.outcome is ReportOutcome.FAILED else "report.generated"
            self.connection.execute(
                "INSERT INTO audit "
                "(audit_id,actor,action,subject_type,subject_id,event_id,details,at) "
                "VALUES (lower(hex(randomblob(16))),?,?,?,?,?,?,?)",
                (
                    record.created_by,
                    action,
                    "report",
                    str(record.report_id),
                    str(record.event_id),
                    json.dumps(
                        {
                            "property_id": str(record.property_id),
                            "period": record.period.label,
                            "outcome": record.outcome.value,
                        },
                        ensure_ascii=False,
                    ),
                    _iso(record.created_at),
                ),
            )
            self.connection.commit()
            return record
        except Exception:
            self.connection.rollback()
            raise

    def record_failure(
        self,
        actor: str,
        property_id: str,
        period: ReportPeriod,
        event_id: str,
        created_at: datetime,
        code: str = "all_sources_unavailable",
        unavailable_sources: list[str] | None = None,
    ) -> None:
        """Audit-only record for a failed generation. No report row is
        written, so the event_id stays free and the same slot can be
        retried once the data recovers.

        ``code`` mirrors the returned error code so the audit trail
        says WHY the attempt failed (all sources unavailable vs.
        authorisation denied). The details carry no exception text."""
        details: dict[str, object] = {
            "property_id": property_id,
            "period": period.label,
            "outcome": "failed",
            "code": code,
        }
        if unavailable_sources is not None:
            details["unavailable_sources"] = unavailable_sources
        self.connection.execute(
            "INSERT INTO audit "
            "(audit_id,actor,action,subject_type,subject_id,event_id,details,at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                new_id(),
                actor,
                "report.failed",
                "property",
                property_id,
                event_id,
                json.dumps(details, ensure_ascii=False),
                _iso(created_at),
            ),
        )
        self.connection.commit()

    @staticmethod
    def _record(row: sqlite3.Row) -> ReportRecord:
        data = json.loads(row["data_json"])
        trigger = (
            ScheduledReportTrigger.model_validate(data["trigger"])
            if row["trigger_kind"] == "scheduled"
            else ManagerReportTrigger.model_validate(data["trigger"])
        )
        return ReportRecord(
            report_id=row["report_id"],
            property_id=row["property_id"],
            period=ReportPeriod(year=row["period_year"], month=row["period_month"]),
            generation=row["generation"],
            trigger=trigger,
            event_id=row["event_id"],
            outcome=ReportOutcome(row["outcome"]),
            maintenance=MaintenanceSectionData.model_validate(data["maintenance"]),
            rent=RentSectionData.model_validate(data["rent"]),
            lease=LeaseSectionData.model_validate(data["lease"]),
            open_tasks=OpenTasksSectionData.model_validate(data["open_tasks"]),
            risks=RisksSectionData.model_validate(data["risks"]),
            narrative=ReportNarrative.model_validate_json(row["narrative_json"]),
            data_as_of=_datetime(row["data_as_of"]),
            created_at=_datetime(row["created_at"]),
            created_by=row["created_by"],
        )


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
