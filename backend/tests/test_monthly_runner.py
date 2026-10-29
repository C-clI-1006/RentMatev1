from __future__ import annotations

import json
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from contracts.reporting import ReportPeriod
from reporting.monthly_runner import previous_month, run_monthly_reports, scheduled_period
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


@pytest.mark.parametrize(
    "instant,expected_period,local_hour",
    [
        # 02:00 on day 1 in Sydney while UTC is still in the prior month.
        ("2026-09-30T16:00:00+00:00", "2026-09", 2),
        ("2026-10-31T15:00:00+00:00", "2026-10", 2),
        ("2026-03-31T15:00:00+00:00", "2026-03", 2),
        ("2026-04-30T16:00:00+00:00", "2026-04", 2),
        # Near midnight: fixed +10/+11 offsets would select the wrong month.
        ("2026-10-31T13:30:00+00:00", "2026-10", 0),
        ("2026-04-30T13:30:00+00:00", "2026-03", 23),
        # DST ends: the Sydney clock repeats the 02:00 hour.
        ("2026-04-04T15:59:00+00:00", "2026-03", 2),
        ("2026-04-04T16:00:00+00:00", "2026-03", 2),
        # DST starts: local time jumps from 01:59 to 03:00.
        ("2026-10-03T15:59:00+00:00", "2026-09", 1),
        ("2026-10-03T16:00:00+00:00", "2026-09", 3),
        # January execution must select December of the previous year.
        ("2026-12-31T15:00:00+00:00", "2026-12", 2),
    ],
)
def test_scheduled_month_uses_sydney_across_utc_boundaries_and_dst(
    instant, expected_period, local_hour
):
    now = datetime.fromisoformat(instant)
    assert now.astimezone(ZoneInfo("Australia/Sydney")).hour == local_hour
    assert scheduled_period(now).label == expected_period
    # An equivalent Sydney-valued datetime must produce the same slot.
    assert scheduled_period(now.astimezone(ZoneInfo("Australia/Sydney"))).label == expected_period


def test_scheduler_rejects_naive_clock():
    with pytest.raises(ValueError, match="timezone-aware"):
        scheduled_period(datetime(2026, 10, 1, 2))


def test_cli_default_uses_sydney_not_host_calendar(monkeypatch, capsys, tmp_path):
    import reporting.monthly_runner as runner

    class FrozenClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 30, 16, tzinfo=timezone.utc)

    captured = []

    def run(database, period, **kwargs):
        captured.append(period.label)
        return {"failed": 0, "period": period.label}

    monkeypatch.setattr(runner, "datetime", FrozenClock)
    monkeypatch.setattr(runner, "run_monthly_reports", run)
    assert runner.main(["--database", str(tmp_path / "unused.db")]) == 0
    assert captured == ["2026-09"]
    assert json.loads(capsys.readouterr().out)["period"] == "2026-09"


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
