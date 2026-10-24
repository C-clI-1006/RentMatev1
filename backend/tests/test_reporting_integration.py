import json
import sqlite3
from datetime import date, datetime, timezone

from fastapi.testclient import TestClient

from contracts.maintenance import MaintenanceStatus, MaintenanceView, Priority
from contracts.reporting import (
    CallerIdentity,
    GenerationRequest,
    ILeaseRentReportSource,
    IMaintenanceReportSource,
    LeaseRentPeriodData,
    LeaseSectionData,
    MaintenanceSectionData,
    ManagerReportTrigger,
    NarrativeSource,
    RentSectionData,
    ReportGenerationError,
    ReportNarrative,
    ReportOutcome,
    ReportPeriod,
    ScheduledReportTrigger,
    SectionAvailability,
)
from lease_rent.reporting import SQLiteLeaseRentReportSource
from maintenance.reporting import SQLiteMaintenanceReportSource
from maintenance.store import Store
from reporting.api import get_connection
from reporting.collector import ReportDataCollector
from reporting.repository import ReportRepository
from reporting.service import ReportService
from shared.sqlite import init_connection

TS = "2026-10-01T00:00:00Z"
PERIOD = ReportPeriod(year=2026, month=10)


def _seed_identity(connection):
    connection.executescript(
        f"""
        INSERT INTO tenant (tenant_id,name,created_at) VALUES ('t1','Tenant','{TS}');
        INSERT INTO property (property_id,name,created_at) VALUES ('p1','Property','{TS}');
        INSERT INTO tenant_property (tenant_id,property_id,created_at)
          VALUES ('t1','p1','{TS}');
        INSERT INTO manager (manager_id,name,created_at) VALUES ('m1','Manager','{TS}');
        INSERT INTO manager_property (manager_id,property_id,created_at)
          VALUES ('m1','p1','{TS}');
        """
    )
    connection.commit()


def _seed_real_sources(path):
    connection = init_connection(path)
    _seed_identity(connection)
    connection.executescript(
        f"""
        INSERT INTO lease (lease_id,property_id,tenant_id,start_date,end_date,version,created_at)
          VALUES ('l1','p1','t1','2026-01-01','2026-11-15','1','{TS}');
        INSERT INTO rent_record
          (rent_record_id,lease_id,property_id,period,due_date,amount_due,amount_paid,status,created_at)
          VALUES ('rr1','l1','p1','2026-10','2026-10-01',1000,0,'overdue','{TS}');
        """
    )
    connection.commit()
    connection.close()

    view = MaintenanceView(
        request_id="mr1",
        tenant_id="t1",
        property_id="p1",
        message_id="msg1",
        title="Gas smell",
        description="Gas smell in kitchen",
        priority=Priority.EMERGENCY,
        status=MaintenanceStatus.IN_PROGRESS,
        version=3,
        emergency_flag=True,
        created_at=datetime(2026, 10, 2, tzinfo=timezone.utc),
        updated_at=datetime(2026, 10, 3, tzinfo=timezone.utc),
    )
    store = Store(path)
    with store.transaction() as db:
        db.execute(
            "INSERT INTO mc_request VALUES (?,?,?,?,?,?,?,?)",
            (
                "mr1",
                "p1",
                "t1",
                "msg1",
                "intake:msg1",
                3,
                view.model_dump_json(),
                json.dumps({"status": "registered", "request_id": "mr1", "persisted_status": "in_progress"}),
            ),
        )


def _live_narrative(_data):
    return ReportNarrative(
        source=NarrativeSource.STUB,
        text="Structured facts summarised by a labelled test double.",
        generated_at=datetime.now(timezone.utc),
    )


def test_real_sqlite_adapters_and_idempotent_generation(tmp_path):
    path = tmp_path / "report.db"
    _seed_real_sources(path)
    connection = init_connection(path)
    try:
        collector = ReportDataCollector(
            SQLiteMaintenanceReportSource(Store(path)),
            SQLiteLeaseRentReportSource(connection),
        )
        service = ReportService(ReportRepository(connection), collector, _live_narrative)
        trigger = ScheduledReportTrigger.for_slot("p1", PERIOD)
        request = GenerationRequest(property_id="p1", period=PERIOD, trigger=trigger)

        first = service.generate(request, CallerIdentity("system:scheduler"))
        second = service.generate(request, CallerIdentity("system:scheduler"))

        assert first.status == "generated" and first.reused is False
        assert second.status == "generated" and second.reused is True
        assert second.report.report_id == first.report.report_id
        assert first.report.outcome is ReportOutcome.COMPLETE
        assert first.report.data_as_of.tzinfo is not None
        assert [item.source for item in first.report.open_tasks.items] == [
            "maintenance",
            "rent",
            "lease",
        ]
        assert {risk.rule_id.value for risk in first.report.risks.risks} == {
            "risk.open_emergency",
            "risk.overdue_rent",
            "risk.lease_expiring",
        }
        assert connection.execute("SELECT COUNT(*) FROM report").fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM audit WHERE action='report.generated'"
        ).fetchone()[0] == 1
    finally:
        connection.close()


class _Maintenance(IMaintenanceReportSource):
    def __init__(self, availability):
        self.availability = availability

    def list_for_period(self, property_id, period):
        return MaintenanceSectionData(
            availability=self.availability,
            note="maintenance unavailable"
            if self.availability is SectionAvailability.NOT_AVAILABLE
            else None,
        )


class _LeaseRent(ILeaseRentReportSource):
    def __init__(self, rent, lease):
        self.rent, self.lease = rent, lease

    def period_summary(self, property_id, period):
        return LeaseRentPeriodData(
            rent=RentSectionData(
                availability=self.rent,
                note="rent unavailable"
                if self.rent is SectionAvailability.NOT_AVAILABLE
                else None,
            ),
            lease=LeaseSectionData(
                availability=self.lease,
                note="lease unavailable"
                if self.lease is SectionAvailability.NOT_AVAILABLE
                else None,
            ),
        )


def _service(connection, maint, rent, lease, narrator=None):
    return ReportService(
        ReportRepository(connection),
        ReportDataCollector(_Maintenance(maint), _LeaseRent(rent, lease)),
        narrator,
    )


def test_partial_data_and_narrative_failure_preserve_structured_report(tmp_path):
    connection = init_connection(tmp_path / "partial.db")
    _seed_identity(connection)
    try:
        service = _service(
            connection,
            SectionAvailability.EMPTY,
            SectionAvailability.NOT_AVAILABLE,
            SectionAvailability.EMPTY,
            narrator=lambda _data: (_ for _ in ()).throw(RuntimeError("offline")),
        )
        request = GenerationRequest(
            property_id="p1",
            period=PERIOD,
            trigger=ManagerReportTrigger(
                manager_id="m1", event_id="manual:partial-1"
            ),
        )
        result = service.generate(request, CallerIdentity("m1"))

        assert result.status == "generated"
        assert result.report.outcome is ReportOutcome.PARTIAL_SUCCESS
        assert result.report.rent.availability is SectionAvailability.NOT_AVAILABLE
        assert result.report.narrative.source is NarrativeSource.MISSING
        assert result.report.narrative.text is None
    finally:
        connection.close()


def test_all_sources_unavailable_stops_and_is_idempotently_audited(tmp_path):
    connection = init_connection(tmp_path / "failed.db")
    _seed_identity(connection)
    try:
        service = _service(
            connection,
            SectionAvailability.NOT_AVAILABLE,
            SectionAvailability.NOT_AVAILABLE,
            SectionAvailability.NOT_AVAILABLE,
        )
        trigger = ScheduledReportTrigger.for_slot("p1", PERIOD)
        request = GenerationRequest(property_id="p1", period=PERIOD, trigger=trigger)

        first = service.generate(request, CallerIdentity("system:scheduler"))
        second = service.generate(request, CallerIdentity("system:scheduler"))

        assert isinstance(first, ReportGenerationError)
        assert first.code == "all_sources_unavailable"
        assert isinstance(second, ReportGenerationError)
        assert connection.execute("SELECT COUNT(*) FROM report").fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM audit WHERE action='report.failed'"
        ).fetchone()[0] == 1
    finally:
        connection.close()


def test_authorization_precedes_event_lookup(tmp_path):
    connection = init_connection(tmp_path / "auth.db")
    _seed_identity(connection)
    try:
        service = _service(
            connection,
            SectionAvailability.EMPTY,
            SectionAvailability.EMPTY,
            SectionAvailability.EMPTY,
            _live_narrative,
        )
        request = GenerationRequest(
            property_id="p1",
            period=PERIOD,
            trigger=ManagerReportTrigger(manager_id="m1", event_id="manual:auth"),
        )
        assert service.generate(request, CallerIdentity("spoofed")).code == "authorization_denied"
        assert service.generate(request, CallerIdentity("m1")).status == "generated"
    finally:
        connection.close()


def test_event_conflict_checked_only_after_property_authorization(tmp_path):
    connection = init_connection(tmp_path / "conflict.db")
    _seed_identity(connection)
    connection.execute(
        "INSERT INTO property (property_id,name,created_at) VALUES ('p2','Other','2026-10-01T00:00:00Z')"
    )
    connection.commit()
    try:
        service = _service(
            connection,
            SectionAvailability.EMPTY,
            SectionAvailability.EMPTY,
            SectionAvailability.EMPTY,
            _live_narrative,
        )
        original = GenerationRequest(
            property_id="p1",
            period=PERIOD,
            trigger=ScheduledReportTrigger(event_id="shared-event"),
        )
        assert service.generate(original, CallerIdentity("system:scheduler")).status == "generated"

        conflicting = GenerationRequest(
            property_id="p2",
            period=PERIOD,
            trigger=ScheduledReportTrigger(event_id="shared-event"),
        )
        result = service.generate(conflicting, CallerIdentity("system:scheduler"))
        assert isinstance(result, ReportGenerationError)
        assert result.code == "event_id_conflict"

        unbound_manager = GenerationRequest(
            property_id="p2",
            period=PERIOD,
            trigger=ManagerReportTrigger(manager_id="m1", event_id="shared-event"),
        )
        denied = service.generate(unbound_manager, CallerIdentity("m1"))
        assert denied.code == "authorization_denied"
    finally:
        connection.close()


def test_scheduler_http_requires_dedicated_token_and_reuses_slot(tmp_path, monkeypatch):
    from main import app

    path = tmp_path / "api.db"
    connection = init_connection(path)
    _seed_identity(connection)
    connection.close()

    def api_connection():
        value = init_connection(path)
        try:
            yield value
        finally:
            value.close()

    app.dependency_overrides[get_connection] = api_connection
    monkeypatch.setenv("RENTMATE_SCHEDULER_TOKEN", "scheduler-secret")
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    try:
        with TestClient(app) as client:
            body = {"property_id": "p1", "year": 2026, "month": 10}
            assert client.post("/api/ops/monthly-reports/run", json=body).status_code == 401
            assert client.post(
                "/api/ops/monthly-reports/run",
                headers={"Authorization": "Bearer wrong"},
                json=body,
            ).status_code == 401
            first = client.post(
                "/api/ops/monthly-reports/run",
                headers={"Authorization": "Bearer scheduler-secret"},
                json=body,
            )
            second = client.post(
                "/api/ops/monthly-reports/run",
                headers={"Authorization": "Bearer scheduler-secret"},
                json=body,
            )

        assert first.status_code == 200
        assert first.json()["report"]["outcome"] == "partial_success"
        assert first.json()["report"]["narrative"]["source"] == "missing"
        assert second.status_code == 200 and second.json()["reused"] is True
    finally:
        app.dependency_overrides.clear()
