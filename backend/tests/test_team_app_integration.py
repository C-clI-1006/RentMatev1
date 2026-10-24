"""Checks for composing the uploaded team branches into one application."""

from fastapi.testclient import TestClient

from contracts.reporting import (
    GenerationRequest,
    LeaseRentPeriodData,
    LeaseSectionData,
    MaintenanceSectionData,
    ManagerReportTrigger,
    RentSectionData,
    ReportGenerated,
    ReportPeriod,
    SectionAvailability,
)
from reporting.collector import ReportDataCollector
from reporting.repository import ReportRepository
from reporting.service import ReportService
from shared.sqlite import init_connection


class EmptyMaintenanceFake:
    """Labelled fake: an accessible maintenance source without records."""

    def list_for_period(self, property_id, period):
        return MaintenanceSectionData(availability=SectionAvailability.EMPTY)


class EmptyLeaseRentFake:
    """Labelled fake: accessible lease/rent sources without records."""

    def period_summary(self, property_id, period):
        return LeaseRentPeriodData(
            rent=RentSectionData(availability=SectionAvailability.EMPTY),
            lease=LeaseSectionData(availability=SectionAvailability.EMPTY),
        )


def test_application_factory_keeps_all_team_routers(tmp_path):
    from main import create_app

    app = create_app(str(tmp_path / "team.db"), str(tmp_path / "evidence"))
    with TestClient(app) as client:
        assert client.get("/health").json() == {"status": "ok"}
        paths = client.get("/openapi.json").json()["paths"]
    for prefix in ("/api/maintenance", "/api/reviews", "/api/evidence"):
        assert any(path.startswith(prefix) for path in paths), prefix
    assert "/api/ops/monthly-reports/run" in paths
    assert "/api/ops/rent-alerts/run" in paths


def test_report_reads_keep_authorisation_and_property_scope_after_reopen(tmp_path, monkeypatch):
    path = tmp_path / "reports.db"
    connection = init_connection(path)
    connection.executescript("""
        INSERT INTO property (property_id,name,created_at)
          VALUES ('p1','Property 1','2026-01-01T00:00:00Z');
        INSERT INTO property (property_id,name,created_at)
          VALUES ('p2','Property 2','2026-01-01T00:00:00Z');
        INSERT INTO manager (manager_id,name,created_at)
          VALUES ('m1','Manager 1','2026-01-01T00:00:00Z');
        INSERT INTO manager (manager_id,name,created_at)
          VALUES ('m2','Manager 2','2026-01-01T00:00:00Z');
        INSERT INTO manager_property (manager_id,property_id,created_at)
          VALUES ('m1','p1','2026-01-01T00:00:00Z');
        INSERT INTO manager_property (manager_id,property_id,created_at)
          VALUES ('m2','p2','2026-01-01T00:00:00Z');
    """)
    connection.commit()
    collector = ReportDataCollector(EmptyMaintenanceFake(), EmptyLeaseRentFake())
    service = ReportService(ReportRepository(connection), collector)
    period = ReportPeriod(year=2026, month=10)
    ids = []
    for event in ("manual-1", "manual-2"):
        result = service.generate(
            GenerationRequest(
                property_id="p1", period=period,
                trigger=ManagerReportTrigger(manager_id="m1", event_id=event),
            ), caller="m1",
        )
        assert isinstance(result, ReportGenerated)
        ids.append(result.report.report_id)
    connection.close()
    connection = init_connection(path)
    try:
        repository = ReportRepository(connection)
        service = ReportService(repository, collector)
        assert service.get(ids[0], "p1", caller="m1").report_id == ids[0]
        assert service.get(ids[0], "p1", caller="m2") is None
        assert service.get(ids[0], "p2", caller="m2") is None
        assert service.list_for_period("p1", period, caller="m2") == []
        reports = service.list_for_period("p1", period, caller="m1")
        assert [report.report_id for report in reports] == ids
        assert [report.generation for report in reports] == [1, 2]

        # Simulate a winner appearing between fast lookup and the atomic insert.
        # Manager 2 may access p2, but must never receive manager 1's p1 report.
        lookup = repository.by_event
        calls = []

        def miss_first_lookup(event_id):
            calls.append(event_id)
            return None if len(calls) == 1 else lookup(event_id)

        monkeypatch.setattr(repository, "by_event", miss_first_lookup)
        result = service.generate(
            GenerationRequest(
                property_id="p2", period=period,
                trigger=ManagerReportTrigger(manager_id="m2", event_id="manual-1"),
            ), caller="m2",
        )
        assert result.status == "failed"
        assert result.code == "event_id_conflict"
        assert connection.execute("SELECT COUNT(*) FROM report").fetchone()[0] == 2
    finally:
        connection.close()
