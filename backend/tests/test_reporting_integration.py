import json
import sqlite3
from datetime import date, datetime, timezone

from fastapi.testclient import TestClient

from contracts.maintenance import (
    MaintenanceEvent,
    MaintenanceStatus,
    MaintenanceView,
    Priority,
)
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
    ReportGenerated,
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
    def __init__(self, availability, raise_exc: Exception | None = None):
        self.availability = availability
        self.raise_exc = raise_exc
        self.calls = 0

    def list_for_period(self, property_id, period):
        self.calls += 1
        if self.raise_exc is not None:
            raise self.raise_exc
        return MaintenanceSectionData(
            availability=self.availability,
            note="maintenance unavailable"
            if self.availability is SectionAvailability.NOT_AVAILABLE
            else None,
        )


class _LeaseRent(ILeaseRentReportSource):
    def __init__(self, rent, lease, raise_exc: Exception | None = None):
        self.rent, self.lease = rent, lease
        self.raise_exc = raise_exc
        self.calls = 0

    def period_summary(self, property_id, period):
        self.calls += 1
        if self.raise_exc is not None:
            raise self.raise_exc
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
        # The missing section keeps NOT_AVAILABLE; the rest of the data is
        # preserved and the gap is annotated rather than hidden.
        assert result.report.rent.availability is SectionAvailability.NOT_AVAILABLE
        assert result.report.maintenance.availability is SectionAvailability.EMPTY
        assert result.report.open_tasks.note == "unavailable sources: rent"
        assert result.report.narrative.source is NarrativeSource.MISSING
        assert result.report.narrative.text is None

        # A partially successful report still owns its event_id: the same
        # request reuses it and never creates a second report row.
        retry = service.generate(request, CallerIdentity("m1"))
        assert retry.status == "generated"
        assert retry.reused is True
        assert retry.report.report_id == result.report.report_id
        assert connection.execute("SELECT COUNT(*) FROM report").fetchone()[0] == 1
    finally:
        connection.close()


def test_all_sources_unavailable_stops_and_is_idempotently_audited(tmp_path):
    connection = init_connection(tmp_path / "failed.db")
    _seed_identity(connection)
    calls: list = []

    def narrator(data):
        calls.append(data)
        return _live_narrative(data)

    try:
        service = _service(
            connection,
            SectionAvailability.NOT_AVAILABLE,
            SectionAvailability.NOT_AVAILABLE,
            SectionAvailability.NOT_AVAILABLE,
            narrator,
        )
        trigger = ScheduledReportTrigger.for_slot("p1", PERIOD)
        request = GenerationRequest(property_id="p1", period=PERIOD, trigger=trigger)

        first = service.generate(request, CallerIdentity("system:scheduler"))
        second = service.generate(request, CallerIdentity("system:scheduler"))

        assert isinstance(first, ReportGenerationError)
        assert first.code == "all_sources_unavailable"
        assert isinstance(second, ReportGenerationError)
        # No report row is written, so the event_id stays free for a retry.
        assert connection.execute("SELECT COUNT(*) FROM report").fetchone()[0] == 0
        # The failure is audit-only, one row per attempt, each carrying the
        # event_id so the audit trail can be tied back to the schedule slot.
        failed = connection.execute(
            "SELECT event_id FROM audit WHERE action='report.failed'"
        ).fetchall()
        assert len(failed) == 2
        assert all(row["event_id"] == trigger.event_id for row in failed)
        # The narrative generator (LLM) is never invoked.
        assert calls == []
    finally:
        connection.close()


def test_slot_retries_after_data_recovers_without_duplicate(tmp_path):
    connection = init_connection(tmp_path / "recover.db")
    _seed_identity(connection)
    try:
        # The fakes start with every source unavailable and recover in
        # place, simulating the underlying data returning for the same
        # schedule slot.
        maint = _Maintenance(SectionAvailability.NOT_AVAILABLE)
        lease_rent = _LeaseRent(SectionAvailability.NOT_AVAILABLE, SectionAvailability.NOT_AVAILABLE)
        service = ReportService(
            ReportRepository(connection),
            ReportDataCollector(maint, lease_rent),
            _live_narrative,
        )
        trigger = ScheduledReportTrigger.for_slot("p1", PERIOD)
        request = GenerationRequest(property_id="p1", period=PERIOD, trigger=trigger)

        failed = service.generate(request, CallerIdentity("system:scheduler"))
        assert isinstance(failed, ReportGenerationError)
        assert failed.code == "all_sources_unavailable"
        assert connection.execute("SELECT COUNT(*) FROM report").fetchone()[0] == 0

        # Data recovers: the SAME event_id now succeeds and is stable.
        maint.availability = SectionAvailability.EMPTY
        lease_rent.rent = SectionAvailability.EMPTY
        lease_rent.lease = SectionAvailability.EMPTY
        recovered = service.generate(request, CallerIdentity("system:scheduler"))
        assert isinstance(recovered, ReportGenerated)
        assert recovered.reused is False
        assert recovered.report.outcome is ReportOutcome.COMPLETE
        assert recovered.report.event_id == trigger.event_id

        stable = service.generate(request, CallerIdentity("system:scheduler"))
        assert stable.reused is True
        assert stable.report.report_id == recovered.report.report_id
        assert connection.execute("SELECT COUNT(*) FROM report").fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM audit WHERE action='report.failed'"
        ).fetchone()[0] == 1
    finally:
        connection.close()


def test_empty_sources_are_not_a_failure_and_complete_the_report(tmp_path):
    connection = init_connection(tmp_path / "empty.db")
    _seed_identity(connection)
    try:
        service = _service(
            connection,
            SectionAvailability.EMPTY,
            SectionAvailability.EMPTY,
            SectionAvailability.EMPTY,
            _live_narrative,
        )
        trigger = ScheduledReportTrigger.for_slot("p1", PERIOD)
        request = GenerationRequest(property_id="p1", period=PERIOD, trigger=trigger)

        result = service.generate(request, CallerIdentity("system:scheduler"))

        # EMPTY means "no records this period", not "data unavailable":
        # the report is generated and is fully complete, not partial.
        assert result.status == "generated"
        assert result.report.outcome is ReportOutcome.COMPLETE
        assert result.report.maintenance.availability is SectionAvailability.EMPTY
        assert result.report.rent.availability is SectionAvailability.EMPTY
        assert result.report.lease.availability is SectionAvailability.EMPTY
        assert connection.execute("SELECT COUNT(*) FROM report").fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM audit WHERE action='report.failed'"
        ).fetchone()[0] == 0
    finally:
        connection.close()


def test_maintenance_source_permission_denial_stops_generation(tmp_path):
    connection = init_connection(tmp_path / "denied-maint.db")
    _seed_identity(connection)
    calls: list = []

    def narrator(data):
        calls.append(data)
        return _live_narrative(data)

    try:
        # The maintenance source refuses access; everything it raises is
        # internal detail that must never leak into the error or audit.
        maint = _Maintenance(
            SectionAvailability.AVAILABLE,
            raise_exc=PermissionError("secret internal reason"),
        )
        lease_rent = _LeaseRent(
            SectionAvailability.AVAILABLE, SectionAvailability.AVAILABLE
        )
        service = ReportService(
            ReportRepository(connection),
            ReportDataCollector(maint, lease_rent),
            narrator,
        )
        trigger = ScheduledReportTrigger.for_slot("p1", PERIOD)
        request = GenerationRequest(property_id="p1", period=PERIOD, trigger=trigger)

        denied = service.generate(request, CallerIdentity("system:scheduler"))

        # Stopped with a generic authorization_denied error.
        assert isinstance(denied, ReportGenerationError)
        assert denied.code == "authorization_denied"
        assert "secret internal reason" not in (denied.detail or "")
        # No report row, no LLM call, and the downstream lease & rent
        # source was never consulted once maintenance refused.
        assert connection.execute("SELECT COUNT(*) FROM report").fetchone()[0] == 0
        assert calls == []
        assert maint.calls == 1
        assert lease_rent.calls == 0
        # The attempt is audit-only, with the generic cause and event_id.
        failed = connection.execute(
            "SELECT event_id, details FROM audit WHERE action='report.failed'"
        ).fetchall()
        assert len(failed) == 1
        assert failed[0]["event_id"] == trigger.event_id
        details = json.loads(failed[0]["details"])
        assert details["outcome"] == "failed"
        assert details["code"] == "authorization_denied"
        assert "secret internal reason" not in failed[0]["details"]
        assert "unavailable_sources" not in details

        # The denial did not claim the slot: once access recovers, the
        # SAME event_id generates and is stable.
        maint.raise_exc = None
        recovered = service.generate(request, CallerIdentity("system:scheduler"))
        assert isinstance(recovered, ReportGenerated)
        assert recovered.reused is False
        assert recovered.report.event_id == trigger.event_id
        stable = service.generate(request, CallerIdentity("system:scheduler"))
        assert stable.reused is True
        assert stable.report.report_id == recovered.report.report_id
        assert connection.execute("SELECT COUNT(*) FROM report").fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM audit WHERE action='report.failed'"
        ).fetchone()[0] == 1
    finally:
        connection.close()


def test_lease_rent_source_permission_denial_stops_generation(tmp_path):
    connection = init_connection(tmp_path / "denied-rent.db")
    _seed_identity(connection)
    calls: list = []

    def narrator(data):
        calls.append(data)
        return _live_narrative(data)

    try:
        maint = _Maintenance(SectionAvailability.AVAILABLE)
        lease_rent = _LeaseRent(
            SectionAvailability.AVAILABLE,
            SectionAvailability.AVAILABLE,
            raise_exc=PermissionError("lease rent access denied"),
        )
        service = ReportService(
            ReportRepository(connection),
            ReportDataCollector(maint, lease_rent),
            narrator,
        )
        trigger = ScheduledReportTrigger.for_slot("p1", PERIOD)
        request = GenerationRequest(property_id="p1", period=PERIOD, trigger=trigger)

        denied = service.generate(request, CallerIdentity("system:scheduler"))

        assert isinstance(denied, ReportGenerationError)
        assert denied.code == "authorization_denied"
        assert "lease rent access denied" not in (denied.detail or "")
        # Maintenance was consulted first, then the refusal stopped
        # everything: no report, no LLM call, audit-only.
        assert maint.calls == 1
        assert lease_rent.calls == 1
        assert connection.execute("SELECT COUNT(*) FROM report").fetchone()[0] == 0
        assert calls == []
        failed = connection.execute(
            "SELECT event_id, details FROM audit WHERE action='report.failed'"
        ).fetchall()
        assert len(failed) == 1
        assert failed[0]["event_id"] == trigger.event_id
        details = json.loads(failed[0]["details"])
        assert details["code"] == "authorization_denied"
        assert "lease rent access denied" not in failed[0]["details"]
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


def test_pre_generation_authorization_denial_is_audited(tmp_path):
    # Authorization is checked before data collection and any LLM call.
    # The failed attempt is audited (trusted caller as actor) without a
    # report row and without claiming the event_id.
    connection = init_connection(tmp_path / "auth-audit.db")
    _seed_identity(connection)
    calls: list = []
    maint = _Maintenance(SectionAvailability.EMPTY)
    lease_rent = _LeaseRent(SectionAvailability.EMPTY, SectionAvailability.EMPTY)

    def narrator(data):
        calls.append(data)
        return _live_narrative(data)

    try:
        service = ReportService(
            ReportRepository(connection),
            ReportDataCollector(maint, lease_rent),
            narrator,
        )
        request = GenerationRequest(
            property_id="p1",
            period=PERIOD,
            trigger=ManagerReportTrigger(manager_id="m1", event_id="manual:auth-audit"),
        )

        # The caller does not match the trigger's manager_id: denied up front.
        denied = service.generate(request, CallerIdentity("spoofed"))
        assert isinstance(denied, ReportGenerationError)
        assert denied.code == "authorization_denied"
        # Nothing was collected and the LLM was never invoked.
        assert maint.calls == 0
        assert lease_rent.calls == 0
        assert calls == []
        assert connection.execute("SELECT COUNT(*) FROM report").fetchone()[0] == 0
        # Exactly one audit-only failure, tied to the trusted caller and
        # the event_id, with the generic cause and no leaked source detail.
        failed = connection.execute(
            "SELECT actor, event_id, details FROM audit WHERE action='report.failed'"
        ).fetchall()
        assert len(failed) == 1
        assert failed[0]["actor"] == "spoofed"
        assert failed[0]["event_id"] == "manual:auth-audit"
        details = json.loads(failed[0]["details"])
        assert details["outcome"] == "failed"
        assert details["code"] == "authorization_denied"
        assert details["property_id"] == "p1"
        assert "unavailable_sources" not in details
    finally:
        connection.close()


def test_first_event_lookup_conflict_is_audited(tmp_path):
    connection = init_connection(tmp_path / "conflict-audit.db")
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
        # A p1 scheduled slot already owns this event id.
        first = service.generate(
            GenerationRequest(
                property_id="p1",
                period=PERIOD,
                trigger=ScheduledReportTrigger(event_id="shared-audit"),
            ),
            CallerIdentity("system:scheduler"),
        )
        assert first.status == "generated"
        # A different property reusing the same event id is rejected at the
        # first lookup and the failed attempt is audited.
        conflicting = service.generate(
            GenerationRequest(
                property_id="p2",
                period=PERIOD,
                trigger=ScheduledReportTrigger(event_id="shared-audit"),
            ),
            CallerIdentity("system:scheduler"),
        )
        assert isinstance(conflicting, ReportGenerationError)
        assert conflicting.code == "event_id_conflict"
        # No extra report row; exactly one failed audit for the attempt,
        # attributed to the trusted caller with the right event/property.
        assert connection.execute("SELECT COUNT(*) FROM report").fetchone()[0] == 1
        failed = connection.execute(
            "SELECT actor, event_id, details FROM audit WHERE action='report.failed'"
        ).fetchall()
        assert len(failed) == 1
        assert failed[0]["actor"] == "system:scheduler"
        assert failed[0]["event_id"] == "shared-audit"
        details = json.loads(failed[0]["details"])
        assert details["code"] == "event_id_conflict"
        assert details["property_id"] == "p2"
    finally:
        connection.close()


def test_concurrent_insert_conflict_is_audited(tmp_path):
    # Simulates the race where the event id is free at the initial lookup
    # but a concurrent request claims it for a different property before
    # our insert. insert() returns the winner; the loser leaves no report
    # row and is audited only.
    connection = init_connection(tmp_path / "race.db")
    _seed_identity(connection)
    try:
        service = _service(
            connection,
            SectionAvailability.EMPTY,
            SectionAvailability.EMPTY,
            SectionAvailability.EMPTY,
            _live_narrative,
        )
        repository = service._repository

        class _Winner:
            property_id = "p2"
            period = PERIOD
            outcome = ReportOutcome.COMPLETE
            report_id = "rep-winner"

        def fake_insert(record):
            return _Winner()

        repository.insert = fake_insert
        request = GenerationRequest(
            property_id="p1",
            period=PERIOD,
            trigger=ManagerReportTrigger(manager_id="m1", event_id="race-event"),
        )

        result = service.generate(request, CallerIdentity("m1"))
        assert isinstance(result, ReportGenerationError)
        assert result.code == "event_id_conflict"
        # The losing request wrote no report row, only the failure audit.
        assert connection.execute("SELECT COUNT(*) FROM report").fetchone()[0] == 0
        failed = connection.execute(
            "SELECT actor, event_id, details FROM audit WHERE action='report.failed'"
        ).fetchall()
        assert len(failed) == 1
        assert failed[0]["actor"] == "m1"
        assert failed[0]["event_id"] == "race-event"
        details = json.loads(failed[0]["details"])
        assert details["code"] == "event_id_conflict"
        assert details["property_id"] == "p1"
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


def _seed_maintenance_only(path, view: MaintenanceView) -> None:
    """Seed identity plus a single maintenance request document (no
    lease/rent rows, so those sources stay EMPTY rather than
    NOT_AVAILABLE)."""
    connection = init_connection(path)
    _seed_identity(connection)
    connection.close()
    store = Store(path)
    with store.transaction() as db:
        db.execute(
            "INSERT INTO mc_request VALUES (?,?,?,?,?,?,?,?)",
            (
                view.request_id,
                "p1",
                view.tenant_id,
                view.message_id,
                f"intake:{view.message_id}",
                view.version,
                view.model_dump_json(),
                json.dumps(
                    {"status": "registered", "request_id": view.request_id,
                     "persisted_status": view.status.value}
                ),
            ),
        )


def test_indeterminate_membership_yields_partial_success_and_persists_flag(tmp_path):
    # A request created before the period with no recorded event before
    # the period end: its membership of this month cannot be confirmed
    # from recorded history. The row is retained and labelled, but the
    # report may only be PARTIAL_SUCCESS, never COMPLETE.
    path = tmp_path / "indeterminate.db"
    _seed_maintenance_only(
        path,
        MaintenanceView(
            request_id="mr-old",
            tenant_id="t1",
            property_id="p1",
            message_id="msg-old",
            title="Old leak",
            description="Leak reported before the period",
            priority=Priority.ROUTINE,
            status=MaintenanceStatus.IN_PROGRESS,
            version=1,
            created_at=datetime(2026, 9, 5, tzinfo=timezone.utc),
            updated_at=datetime(2026, 9, 5, tzinfo=timezone.utc),
        ),
    )
    connection = init_connection(path)
    try:
        collector = ReportDataCollector(
            SQLiteMaintenanceReportSource(Store(path)),
            SQLiteLeaseRentReportSource(connection),
        )
        service = ReportService(
            ReportRepository(connection), collector, _live_narrative
        )
        trigger = ScheduledReportTrigger.for_slot("p1", PERIOD)
        request = GenerationRequest(
            property_id="p1", period=PERIOD, trigger=trigger
        )
        result = service.generate(request, CallerIdentity("system:scheduler"))

        assert result.status == "generated"
        assert result.report.outcome is ReportOutcome.PARTIAL_SUCCESS
        # Available-but-uncertain maintenance is NOT unavailable: the row
        # and its note are kept, and no Q5 all-sources failure is written.
        assert result.report.maintenance.availability is SectionAvailability.AVAILABLE
        assert [row.request_id for row in result.report.maintenance.requests] == ["mr-old"]
        assert "mr-old" in (result.report.maintenance.note or "")
        assert result.report.maintenance.period_range_complete is False
        # EMPTY (no records this period) is not NOT_AVAILABLE.
        assert result.report.rent.availability is SectionAvailability.EMPTY
        assert result.report.lease.availability is SectionAvailability.EMPTY
        assert connection.execute("SELECT COUNT(*) FROM report").fetchone()[0] == 1
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM audit WHERE action='report.failed'"
            ).fetchone()[0]
            == 0
        )
        # The flag comes from the stored payload and survives a read back.
        report = ReportRepository(connection).get(result.report.report_id, "p1")
        assert report.maintenance.period_range_complete is False
        assert report.outcome is ReportOutcome.PARTIAL_SUCCESS
    finally:
        connection.close()

    # A fresh connection proves the flag round-trips through SQLite.
    reopened = init_connection(path)
    try:
        report = ReportRepository(reopened).get(result.report.report_id, "p1")
        assert report.maintenance.period_range_complete is False
    finally:
        reopened.close()


def test_confirmed_membership_keeps_report_complete(tmp_path):
    # A request created in the month, or one with an in-month event,
    # already confirms membership; missing earlier history must not
    # demote the report.
    base = dict(
        tenant_id="t1",
        property_id="p1",
        title="Confirmed",
        description="Created or changed inside the period",
        priority=Priority.ROUTINE,
        status=MaintenanceStatus.IN_PROGRESS,
        version=1,
    )
    in_month = MaintenanceView(
        request_id="mr-new",
        message_id="msg-new",
        created_at=datetime(2026, 10, 2, tzinfo=timezone.utc),
        updated_at=datetime(2026, 10, 2, tzinfo=timezone.utc),
        **base,
    )
    changed_in_month = MaintenanceView(
        request_id="mr-changed",
        message_id="msg-changed",
        created_at=datetime(2026, 8, 20, tzinfo=timezone.utc),
        updated_at=datetime(2026, 10, 15, tzinfo=timezone.utc),
        events=[
            MaintenanceEvent(
                event_id="evt-1",
                request_id="mr-changed",
                property_id="p1",
                occurred_at=datetime(2026, 10, 15, tzinfo=timezone.utc),
                actor="manager:m1",
                from_status=MaintenanceStatus.SUBMITTED,
                to_status=MaintenanceStatus.IN_PROGRESS,
            )
        ],
        **base,
    )

    for name, view in (("month-new.db", in_month), ("month-changed.db", changed_in_month)):
        path = tmp_path / name
        _seed_maintenance_only(path, view)
        connection = init_connection(path)
        try:
            collector = ReportDataCollector(
                SQLiteMaintenanceReportSource(Store(path)),
                SQLiteLeaseRentReportSource(connection),
            )
            service = ReportService(
                ReportRepository(connection), collector, _live_narrative
            )
            trigger = ScheduledReportTrigger.for_slot("p1", PERIOD)
            request = GenerationRequest(
                property_id="p1", period=PERIOD, trigger=trigger
            )
            result = service.generate(request, CallerIdentity("system:scheduler"))

            assert result.report.outcome is ReportOutcome.COMPLETE
            assert result.report.maintenance.availability is SectionAvailability.AVAILABLE
            assert result.report.maintenance.period_range_complete is True
            assert [row.request_id for row in result.report.maintenance.requests] == [
                view.request_id
            ]
        finally:
            connection.close()


def test_legacy_report_row_without_flag_stays_complete(tmp_path):
    # Rows written before period_range_complete existed lack the key in
    # data_json; reading them must fall back to the default (complete)
    # and keep their recorded outcome.
    path = tmp_path / "legacy.db"
    _seed_real_sources(path)
    connection = init_connection(path)
    try:
        collector = ReportDataCollector(
            SQLiteMaintenanceReportSource(Store(path)),
            SQLiteLeaseRentReportSource(connection),
        )
        service = ReportService(
            ReportRepository(connection), collector, _live_narrative
        )
        trigger = ScheduledReportTrigger.for_slot("p1", PERIOD)
        request = GenerationRequest(
            property_id="p1", period=PERIOD, trigger=trigger
        )
        result = service.generate(request, CallerIdentity("system:scheduler"))
        assert result.report.outcome is ReportOutcome.COMPLETE

        # Simulate a pre-existing row: drop the flag from the stored payload.
        row = connection.execute(
            "SELECT data_json FROM report WHERE report_id=?",
            (result.report.report_id,),
        ).fetchone()
        data = json.loads(row["data_json"])
        data["maintenance"].pop("period_range_complete", None)
        connection.execute(
            "UPDATE report SET data_json=? WHERE report_id=?",
            (json.dumps(data), result.report.report_id),
        )
        connection.commit()

        report = ReportRepository(connection).get(result.report.report_id, "p1")
        assert report.outcome is ReportOutcome.COMPLETE
        assert report.maintenance.period_range_complete is True
    finally:
        connection.close()


def test_manager_and_scheduler_providers_agree_on_range_completeness(tmp_path):
    from maintenance.reporting import MaintenanceReportSource

    def _section_pair(path):
        class _Auth:
            property_id = "p1"

        class _ManagerService:
            auth = _Auth()

            def require_manager_access(self):
                return None

            def list_requests(self):
                store = Store(path)
                with store.transaction() as db:
                    rows = db.execute(
                        "SELECT document FROM mc_request WHERE property_id='p1'"
                    ).fetchall()
                return [
                    MaintenanceView.model_validate_json(row["document"])
                    for row in rows
                ]

        manager = MaintenanceReportSource(_ManagerService()).list_for_period("p1", PERIOD)
        scheduler = SQLiteMaintenanceReportSource(
            Store(path)
        ).list_for_period("p1", PERIOD)
        return manager, scheduler

    # Uncertain membership: both providers flag the incomplete range.
    indeterminate = MaintenanceView(
        request_id="mr-old",
        tenant_id="t1",
        property_id="p1",
        message_id="msg-old",
        title="Old leak",
        description="Leak reported before the period",
        priority=Priority.ROUTINE,
        status=MaintenanceStatus.IN_PROGRESS,
        version=1,
        created_at=datetime(2026, 9, 5, tzinfo=timezone.utc),
        updated_at=datetime(2026, 9, 5, tzinfo=timezone.utc),
    )
    indeterminate_path = tmp_path / "indeterminate.db"
    _seed_maintenance_only(indeterminate_path, indeterminate)
    manager, scheduler = _section_pair(indeterminate_path)
    assert manager.period_range_complete is False
    assert scheduler.period_range_complete is False
    assert manager.period_range_complete == scheduler.period_range_complete
    assert [r.request_id for r in manager.requests] == [
        r.request_id for r in scheduler.requests
    ]
    assert manager.note == scheduler.note

    # Confirmed membership: both providers keep the range complete.
    confirmed = MaintenanceView(
        request_id="mr-new",
        tenant_id="t1",
        property_id="p1",
        message_id="msg-new",
        title="New leak",
        description="Leak reported in the period",
        priority=Priority.ROUTINE,
        status=MaintenanceStatus.IN_PROGRESS,
        version=1,
        created_at=datetime(2026, 10, 2, tzinfo=timezone.utc),
        updated_at=datetime(2026, 10, 2, tzinfo=timezone.utc),
    )
    confirmed_path = tmp_path / "confirmed.db"
    _seed_maintenance_only(confirmed_path, confirmed)
    manager, scheduler = _section_pair(confirmed_path)
    assert manager.period_range_complete is True
    assert scheduler.period_range_complete is True
    assert manager.period_range_complete == scheduler.period_range_complete
    assert [r.request_id for r in manager.requests] == [
        r.request_id for r in scheduler.requests
    ]
