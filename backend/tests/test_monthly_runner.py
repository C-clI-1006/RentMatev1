from __future__ import annotations

import json
from datetime import date, datetime, timezone

from contracts.reporting import ReportPeriod
from reporting.monthly_runner import previous_month, run_monthly_reports
from shared.sqlite import init_connection


def _seed_property(path, property_id: str) -> None:
    connection = init_connection(path)
    connection.execute(
        "INSERT INTO property(property_id,name,address,created_at) VALUES (?,?,?,?)",
        (
            property_id,
            property_id + " name",
            property_id + " address",
            "2026-01-01T00:00:00Z",
        ),
    )
    connection.commit()
    connection.close()


def test_monthly_runner_defaults_to_previous_calendar_month():
    assert previous_month(date(2026, 10, 29)) == ReportPeriod(year=2026, month=9)
    assert previous_month(date(2026, 1, 1)) == ReportPeriod(year=2025, month=12)


def test_monthly_runner_generates_all_properties_and_records_idempotent_retry(
    tmp_path, monkeypatch
):
    database = tmp_path / "runner.db"
    log = tmp_path / "runs.jsonl"
    _seed_property(database, "p2")
    _seed_property(database, "p1")
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    period = ReportPeriod(year=2026, month=9)

    first = run_monthly_reports(
        database,
        period,
        log_path=log,
        now=datetime(2026, 10, 1, 2, tzinfo=timezone.utc),
    )
    second = run_monthly_reports(
        database,
        period,
        log_path=log,
        now=datetime(2026, 10, 1, 2, 5, tzinfo=timezone.utc),
    )

    assert first["property_count"] == 2
    assert first["succeeded"] == 2 and first["failed"] == 0
    assert [item["property_id"] for item in first["results"]] == ["p1", "p2"]
    assert all(item["reused"] is False for item in first["results"])
    assert second["succeeded"] == 2 and second["failed"] == 0
    assert all(item["reused"] is True for item in second["results"])

    records = [json.loads(line) for line in log.read_text().splitlines()]
    assert len(records) == 2
    assert records[0]["period"] == "2026-09"
    assert "token" not in log.read_text().lower()

    connection = init_connection(database)
    try:
        assert connection.execute("SELECT COUNT(*) FROM report").fetchone()[0] == 2
    finally:
        connection.close()
