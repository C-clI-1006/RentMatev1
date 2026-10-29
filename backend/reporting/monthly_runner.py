"""Trusted command-line runner for the automatic monthly report task.

The operating-system scheduler invokes this module.  It deliberately reuses
the same scheduled trigger and ReportService as the HTTP operations endpoint,
so retries retain the existing per-property/month idempotency guarantee.
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Sequence
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

from contracts.reporting import (  # noqa: E402
    GenerationRequest,
    ReportGenerated,
    ReportGenerationError,
    ReportPeriod,
    REPORTING_TIMEZONE,
    ScheduledReportTrigger,
)
from lease_rent.reporting import SQLiteLeaseRentReportSource  # noqa: E402
from llm.reporting import LlmReportNarrator  # noqa: E402
from maintenance.reporting import SQLiteMaintenanceReportSource  # noqa: E402
from maintenance.store import Store  # noqa: E402
from reporting.collector import ReportDataCollector  # noqa: E402
from reporting.repository import ReportRepository  # noqa: E402
from reporting.service import ReportService  # noqa: E402
from shared.sqlite import init_connection  # noqa: E402

BUSINESS_TIMEZONE = REPORTING_TIMEZONE


def scheduled_period(now: datetime | None = None) -> ReportPeriod:
    """Last completed Sydney calendar month, independent of the host timezone."""
    instant = now if now is not None else datetime.now(timezone.utc)
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("Scheduler time must be timezone-aware")
    return previous_month(instant.astimezone(ZoneInfo(BUSINESS_TIMEZONE)).date())


def previous_month(today: date) -> ReportPeriod:
    """Return the last completed calendar month."""
    if today.month == 1:
        return ReportPeriod(year=today.year - 1, month=12)
    return ReportPeriod(year=today.year, month=today.month - 1)


def run_monthly_reports(
    database: Path,
    period: ReportPeriod,
    *,
    log_path: Path | None = None,
    now: datetime | None = None,
) -> dict[str, object]:
    """Generate one scheduled report for every persisted property.

    A failure for one property is recorded and does not prevent the remaining
    properties from running.  The returned/logged record never contains
    credentials, exception text, report content or model prompts.
    """
    started_at = now or datetime.now(timezone.utc)
    connection = init_connection(database)
    try:
        property_ids = [
            str(row[0])
            for row in connection.execute(
                "SELECT property_id FROM property ORDER BY property_id"
            ).fetchall()
        ]
        service = ReportService(
            ReportRepository(connection),
            ReportDataCollector(
                SQLiteMaintenanceReportSource(Store(database)),
                SQLiteLeaseRentReportSource(connection),
            ),
            LlmReportNarrator(),
        )
        results: list[dict[str, object]] = []
        for property_id in property_ids:
            trigger = ScheduledReportTrigger.for_slot(property_id, period)
            try:
                result = service.generate(
                    GenerationRequest(
                        property_id=property_id,
                        period=period,
                        trigger=trigger,
                    ),
                    caller="system:scheduler",
                )
                if isinstance(result, ReportGenerated):
                    results.append(
                        {
                            "property_id": property_id,
                            "status": "succeeded",
                            "report_id": str(result.report.report_id),
                            "outcome": result.report.outcome.value,
                            "reused": result.reused,
                        }
                    )
                else:
                    assert isinstance(result, ReportGenerationError)
                    results.append(
                        {
                            "property_id": property_id,
                            "status": "failed",
                            "code": result.code,
                        }
                    )
            except Exception:
                # Continue the batch without leaking internal exception text to
                # the durable execution record.  The non-zero exit status tells
                # operators that investigation is required.
                results.append(
                    {
                        "property_id": property_id,
                        "status": "failed",
                        "code": "unexpected_error",
                    }
                )
    finally:
        connection.close()

    record: dict[str, object] = {
        "started_at": _iso(started_at),
        "finished_at": _iso(datetime.now(timezone.utc)),
        "period": period.label,
        "business_timezone": BUSINESS_TIMEZONE,
        "property_count": len(property_ids),
        "succeeded": sum(item["status"] == "succeeded" for item in results),
        "failed": sum(item["status"] == "failed" for item in results),
        "results": results,
    }
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate scheduled monthly reports")
    parser.add_argument(
        "--database",
        type=Path,
        default=Path(os.getenv("DATABASE_PATH", "rentmate.db")),
    )
    parser.add_argument("--log", type=Path)
    parser.add_argument("--year", type=int)
    parser.add_argument("--month", type=int)
    args = parser.parse_args(argv)
    if (args.year is None) != (args.month is None):
        parser.error("--year and --month must be supplied together")
    try:
        period = (
            ReportPeriod(year=args.year, month=args.month)
            if args.year is not None
            else scheduled_period()
        )
    except ValueError as error:
        parser.error(str(error))

    record = run_monthly_reports(args.database, period, log_path=args.log)
    print(json.dumps(record, ensure_ascii=False))
    return 1 if record["failed"] else 0


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


if __name__ == "__main__":
    raise SystemExit(main())
