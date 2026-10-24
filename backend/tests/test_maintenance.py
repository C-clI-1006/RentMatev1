import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC

import pytest
from fastapi.testclient import TestClient

from contracts.maintenance import MaintenanceIntake, MaintenanceStatusCommand, Priority
from contracts.maintenance import MaintenanceStatus as S
from llm import Assessment
from maintenance.outbox import RetryableHandoffError, dispatch_pending, reconcile
from maintenance.rules import emergency_indicators
from maintenance.seed_demo import seed
from maintenance.service import MaintenanceService
from maintenance.store import Store
from permissions import ManagerAccessDeniedError
from shared import AuthContext, utcnow
from shared.sqlite import init_connection


@pytest.mark.parametrize(
    "description,expected",
    [
        ("No gas smell, smoke or fire.", False),
        ("No hot water. Cold water is available.", False),
        ("There is no smoke but a strong gas smell.", True),
        ("No gas smell. Smoke is filling the room.", True),
        ("Strong gas smell and hissing from the pipe.", True),
        ("No heating in winter.", True),
    ],
)
def test_explicit_hazard_denial_and_contrast(description, expected):
    assert bool(emergency_indicators(description)) is expected


class Assessor:
    def assess(self, **kwargs):
        return Assessment(
            Priority.ROUTINE, "Loose handle, no emergency signs", 0.8, False, source="stub"
        )


@pytest.fixture
def setup(tmp_path):
    path = tmp_path / "test.db"
    db = init_connection(path)
    ts = "2026-01-01T00:00:00Z"
    db.executescript(
        f"""
        INSERT INTO tenant (tenant_id,name,created_at) VALUES
          ('t1','Tenant 1','{ts}'), ('t2','Tenant 2','{ts}');
        INSERT INTO property (property_id,name,created_at) VALUES
          ('p1','Property 1','{ts}'), ('p2','Property 2','{ts}');
        INSERT INTO tenant_property (tenant_id,property_id,created_at) VALUES
          ('t1','p1','{ts}'), ('t2','p2','{ts}');
        INSERT INTO manager (manager_id,name,created_at) VALUES
          ('m1','Manager 1','{ts}'), ('m2','Manager 2','{ts}');
        INSERT INTO manager_property (manager_id,property_id,created_at) VALUES
          ('m1','p1','{ts}'), ('m2','p2','{ts}');
        INSERT INTO evidence_document
          (document_id,tenant_id,property_id,original_filename,file_type,size_bytes,
           storage_ref,upload_status,created_at,updated_at)
        VALUES ('ev1','t1','p1','completion.txt','txt',1,
                'evidence/ev1.txt','saved','{ts}','{ts}');
        """
    )
    db.commit()
    db.close()
    store = Store(path)
    tenant = MaintenanceService(store, Assessor(), AuthContext("t1", "p1"))
    manager = MaintenanceService(store, Assessor(), AuthContext("t1", "p1", "m1"))
    return store, tenant, manager


def intake(**overrides):
    values = {
        "message_id": "msg1",
        "event_id": "e1",
        "tenant_id": "t1",
        "property_id": "p1",
        "title": "Handle",
        "description": "Loose handle",
        "submitted_at": utcnow(),
    }
    values.update(overrides)
    return MaintenanceIntake(**values)


def command(manager, rid, status, version, **kw):
    return manager.change_status(
        MaintenanceStatusCommand(
            request_id=rid,
            target_status=status,
            expected_version=version,
            manager_id="m1",
            reason="Manager confirmed action",
            **kw,
        )
    )


def test_complete_lifecycle_persistence_and_outbox(setup):
    store, t, m = setup
    r = t.submit_issue(intake())
    assert r.persisted_status == S.ASSESSED
    version = 2
    for state in [S.APPROVED, S.IN_PROGRESS, S.AWAITING_CONFIRMATION, S.CLOSED]:
        out = command(m, r.request_id, state, version, evidence_refs=["ev1"])
        assert out.status == "applied"
        version = out.version
    loaded = MaintenanceService(Store(store.path), Assessor(), m.auth).get(r.request_id, "p1")
    assert loaded.status == S.CLOSED and len(loaded.events) == 6
    with store.transaction() as db:
        assert db.execute("SELECT count(*) FROM mc_outbox").fetchone()[0] == 12


def test_duplicate_by_source_even_new_event(setup):
    _store, t, m = setup
    a = t.submit_issue(intake())
    b = t.submit_issue(intake(event_id="different"))
    assert a == b
    assert len(m.list_requests()) == 1


def test_duplicate_concurrent(setup):
    _store, t, m = setup
    with ThreadPoolExecutor(2) as pool:
        values = list(pool.map(lambda _: t.submit_issue(intake()), range(2)))
    assert values[0].request_id == values[1].request_id
    assert len(m.list_requests()) == 1


def test_missing_data_and_scope(setup):
    store, t, _m = setup
    assert t.submit_issue(intake(description=" ")).status == "needs_clarification"
    assert t.submit_issue(intake(property_id="other")).code == "forbidden"
    r = t.submit_issue(intake())
    other = MaintenanceService(store, Assessor(), AuthContext("t2", "p1"))
    assert other.get(r.request_id, "p1") is None
    alien = MaintenanceService(store, Assessor(), AuthContext("t1", "p2", "m2"))
    assert alien.get(r.request_id, "p2") is None
    assert t.get(r.request_id, "p2") is None


def test_manager_scope_uses_authoritative_property_binding(setup):
    store, tenant, _manager = setup
    request = tenant.submit_issue(intake())
    unbound = MaintenanceService(store, Assessor(), AuthContext("t1", "p1", "m2"))
    unknown = MaintenanceService(store, Assessor(), AuthContext("t1", "p1", "unknown"))

    for service in (unbound, unknown):
        with pytest.raises(ManagerAccessDeniedError):
            service.list_requests()
        with pytest.raises(ManagerAccessDeniedError):
            service.get(request.request_id, "p1")
        result = service.change_status(
            MaintenanceStatusCommand(
                request_id=request.request_id,
                target_status=S.APPROVED,
                expected_version=2,
                manager_id=service.auth.manager_id,
                reason="Must not be applied",
            )
        )
        assert result.code == "forbidden"


def test_guards_and_stale_update(setup):
    _store, t, m = setup
    r = t.submit_issue(intake())
    assert command(m, r.request_id, S.CLOSED, 2).code == "illegal_transition"
    assert command(m, r.request_id, S.APPROVED, 1).code == "version_mismatch"
    assert command(t, r.request_id, S.APPROVED, 2).code == "forbidden"
    command(m, r.request_id, S.APPROVED, 2)
    command(m, r.request_id, S.IN_PROGRESS, 3)
    assert command(m, r.request_id, S.AWAITING_CONFIRMATION, 4).code == "evidence_required"
    command(m, r.request_id, S.AWAITING_CONFIRMATION, 4, evidence_refs=["ev1"])
    assert command(m, r.request_id, S.CLOSED, 5).code == "evidence_required"
    assert command(m, r.request_id, S.IN_PROGRESS, 5).status == "applied"


def test_conflicting_managers_only_one_wins(setup):
    _store, t, m = setup
    r = t.submit_issue(intake())
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: command(m, r.request_id, S.APPROVED, 2), range(2)))
    assert sorted(x.status for x in results) == ["applied", "failed"]
    assert m.get(r.request_id, "p1").version == 3


def test_failure_review_manual_and_emergency(setup):
    store, t, m = setup

    class Bad:
        def assess(self, **kw):
            raise RuntimeError("offline")

    t.assessor = Bad()
    r = t.submit_issue(intake(title="Gas leak", description="Gas leak in kitchen"))
    assert r.persisted_status == S.UNDER_REVIEW and r.priority is None
    with store.transaction() as db:
        assert (
            db.execute("SELECT count(*) FROM mc_outbox WHERE target='manager_alert'").fetchone()[0]
            == 1
        )
    assert (
        m.manual_assessment(r.request_id, 2, Priority.EMERGENCY, "Confirmed gas emergency").status
        == "applied"
    )
    assert command(m, r.request_id, S.APPROVED, 3).status == "applied"


def test_late_assessment_does_not_reopen_cancelled(setup):
    _store, t, m = setup
    entered, release = threading.Event(), threading.Event()

    class Slow(Assessor):
        def assess(self, **kw):
            entered.set()
            assert release.wait(5)
            return super().assess(**kw)

    t.assessor = Slow()
    with ThreadPoolExecutor(1) as pool:
        f = pool.submit(t.submit_issue, intake())
        assert entered.wait(5)
        v = m.list_requests()[0]
        assert command(m, v.request_id, S.CANCELLED, 1).status == "applied"
        release.set()
        r = f.result()
    assert r.persisted_status == S.CANCELLED
    assert m.get(r.request_id, "p1").status == S.CANCELLED


def test_handoff_retry_and_ambiguous_receipt(setup):
    store, t, m = setup
    r = t.submit_issue(intake())

    def fail(event):
        raise RetryableHandoffError()

    out = dispatch_pending(store, {"communication": fail})
    assert out[0]["status"] == "retry"
    assert m.get(r.request_id, "p1").status == S.ASSESSED
    seen = []

    def ok(event):
        seen.append(event["event_id"])
        return True

    dispatch_pending(store, {"communication": ok})
    assert len(seen) == 2 and len(set(seen)) == 2

    def ambiguous(event):
        raise TimeoutError()

    out = dispatch_pending(store, {"evidence": ambiguous}, limit=1)
    assert out[0]["status"] == "uncertain"
    eid = out[0]["event_id"]
    reconcile(store, eid, "evidence", accepted=True)
    with store.transaction() as db:
        assert (
            db.execute(
                "SELECT status FROM mc_outbox WHERE event_id=? AND target='evidence'", (eid,)
            ).fetchone()[0]
            == "handed_off"
        )


def test_api_auth_identity_and_manager_flow(setup, monkeypatch):
    from main import app
    from maintenance.api import get_store

    store, _t, _m = setup
    app.dependency_overrides[get_store] = lambda: store
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.setenv(
        "RENTMATE_AUTH_TOKENS",
        json.dumps(
            {
                "tenant-secret": {"tenant_id": "t1", "property_id": "p1"},
                "manager-secret": {"tenant_id": "t1", "property_id": "p1", "manager_id": "m1"},
                "unbound-manager": {"tenant_id": "t1", "property_id": "p1", "manager_id": "m2"},
                "unknown-manager": {"tenant_id": "t1", "property_id": "p1", "manager_id": "missing"},
                "unbound-tenant": {"tenant_id": "t2", "property_id": "p1"},
            }
        ),
    )
    try:
        with TestClient(app) as c:
            assert c.get("/api/maintenance/requests").status_code in (401, 403)
            h = {"Authorization": "Bearer tenant-secret"}
            b = {
                "title": "Handle",
                "description": "Loose handle",
            }
            assert (
                c.post(
                    "/api/maintenance/requests", json={**b, "property_id": "spoof"}, headers=h
                ).status_code
                == 422
            )
            assert (
                c.post(
                    "/api/maintenance/requests",
                    json={**b, "message_id": "caller-controlled", "event_id": "caller-controlled"},
                    headers=h,
                ).status_code
                == 422
            )
            r = c.post("/api/maintenance/requests", json=b, headers=h)
            assert r.status_code == 200
            keyed = {**h, "Idempotency-Key": "replay-test"}
            first = c.post("/api/maintenance/requests", json=b, headers=keyed)
            again = c.post("/api/maintenance/requests", json=b, headers=keyed)
            assert first.status_code == again.status_code == 200
            assert first.json() == again.json()
            conflict = c.post("/api/maintenance/requests", json={**b, "title": "Changed"}, headers=keyed)
            assert conflict.status_code == 409
            assert len(c.get("/api/maintenance/requests", headers=h).json()) == 2
            rid = r.json()["request_id"]
            assert c.get("/api/maintenance/outbox", headers=h).status_code == 403
            assert (
                c.post(
                    f"/api/maintenance/requests/{rid}/status",
                    headers=h,
                    json={"target_status": "approved", "expected_version": 2, "reason": "Approve"},
                ).status_code
                == 403
            )
            mh = {"Authorization": "Bearer manager-secret"}
            for token in ("unbound-manager", "unknown-manager", "unbound-tenant"):
                assert (
                    c.get(
                        "/api/maintenance/requests",
                        headers={"Authorization": "Bearer " + token},
                    ).status_code
                    == 403
                )
            assert (
                c.post(
                    f"/api/maintenance/requests/{rid}/assessment",
                    headers=mh,
                    json={
                        "expected_version": 2,
                        "priority": "routine",
                        "rationale": "Manager inspected handle",
                    },
                ).status_code
                == 200
            )
            assert c.get("/api/maintenance/summaries", headers=mh).json()[0]["request_id"] == rid
    finally:
        app.dependency_overrides.clear()


def test_demo_seed_creates_authoritative_identity_bindings(tmp_path):
    database = tmp_path / "demo.db"
    seed(database)
    db = init_connection(database)
    try:
        assert db.execute(
            "SELECT 1 FROM tenant_property WHERE tenant_id=? AND property_id=?",
            ("demo-tenant", "demo-property"),
        ).fetchone()
        assert db.execute(
            "SELECT 1 FROM manager_property WHERE manager_id=? AND property_id=?",
            ("demo-manager", "demo-property"),
        ).fetchone()
    finally:
        db.close()


def test_commit_rolls_back_when_queue_write_fails(setup):
    store, t, _m = setup
    with store.transaction() as db:
        db.execute(
            "CREATE TRIGGER reject_outbox BEFORE INSERT ON mc_outbox BEGIN SELECT RAISE(ABORT,'injected failure'); END"
        )
    with pytest.raises(Exception, match="injected failure"):
        t.submit_issue(intake())
    with store.transaction() as db:
        assert db.execute("SELECT count(*) FROM mc_request").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM mc_history").fetchone()[0] == 0


def test_restart_during_assessment_allows_manual_recovery(setup):
    _store, t, m = setup
    entered, release = threading.Event(), threading.Event()

    class Slow(Assessor):
        def assess(self, **kw):
            entered.set()
            assert release.wait(5)
            return super().assess(**kw)

    t.assessor = Slow()
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(t.submit_issue, intake())
        assert entered.wait(5)
        v = m.list_requests()[0]
        assert (
            m.manual_assessment(v.request_id, 1, Priority.URGENT, "Manual recovery").status
            == "applied"
        )
        release.set()
        future.result()
    assert m.get(v.request_id, "p1").priority == Priority.URGENT


def test_report_provider_scope_unknown_and_storage_failure(setup):
    from contracts.reporting import ReportPeriod
    from contracts.reporting import SectionAvailability as A
    from maintenance.reporting import MaintenanceReportSource
    store, tenant, manager = setup
    source = MaintenanceReportSource(manager)
    period = ReportPeriod(year=utcnow().year, month=utcnow().month)
    assert source.list_for_period("p1", period).availability == A.EMPTY
    with pytest.raises(PermissionError):
        source.list_for_period("p2", period)
    with pytest.raises(PermissionError):
        MaintenanceReportSource(tenant).list_for_period("p1", period)
    result = tenant.submit_issue(intake())
    with store.transaction() as db:
        row = db.execute("SELECT document FROM mc_request WHERE id=?", (result.request_id,)).fetchone()
        document = json.loads(row[0])
        document["priority"] = None
        db.execute("UPDATE mc_request SET document=? WHERE id=?", (json.dumps(document), result.request_id))
    section = source.list_for_period("p1", period)
    assert section.availability == A.AVAILABLE
    assert section.requests[0].priority is None
    assert section.requests[0].references[0].source_id == result.request_id
    with store.transaction() as db:
        db.execute("UPDATE mc_request SET document='invalid'")
    assert source.list_for_period("p1", period).availability == A.NOT_AVAILABLE


def test_report_period_history_and_exclusive_boundary(setup, monkeypatch):
    from datetime import datetime

    from contracts.reporting import ReportPeriod
    from contracts.reporting import SectionAvailability as A
    from maintenance.reporting import MaintenanceReportSource
    _store, tenant, manager = setup
    clock = [datetime(2026, 9, 1, tzinfo=UTC)]
    monkeypatch.setattr("maintenance.service.utcnow", lambda: clock[0])
    old = tenant.submit_issue(intake())
    clock[0] = datetime(2026, 11, 1, tzinfo=UTC)
    assert command(manager, old.request_id, S.CANCELLED, 2).status == "applied"
    future = tenant.submit_issue(intake(message_id="future", event_id="future"))
    source = MaintenanceReportSource(manager)
    october = source.list_for_period("p1", ReportPeriod(year=2026, month=10))
    assert [r.request_id for r in october.requests] == [old.request_id]
    assert october.requests[0].status == S.CANCELLED  # current value, not historical snapshot
    november = source.list_for_period("p1", ReportPeriod(year=2026, month=11))
    assert {r.request_id for r in november.requests} == {old.request_id, future.request_id}
    assert source.list_for_period("p1", ReportPeriod(year=2026, month=8)).availability == A.EMPTY


def test_strict_replay_concurrent_and_payload_conflict(setup):
    store, tenant, _manager = setup
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: tenant.submit_issue(intake(), strict_replay=True), range(4)))
    assert len({r.request_id for r in results}) == 1
    assert tenant.submit_issue(intake(evidence_refs=["changed"]), strict_replay=True).code == "idempotency_conflict"
    with store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM mc_request").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM mc_history").fetchone()[0] == 2


def test_collector_with_real_maintenance_provider(setup):
    from contracts.reporting import (
        LeaseRentPeriodData,
        LeaseSectionData,
        RentSectionData,
        ReportPeriod,
    )
    from contracts.reporting import (
        SectionAvailability as A,
    )
    from maintenance.reporting import MaintenanceReportSource
    from reporting.collector import ReportDataCollector
    store, tenant, manager = setup
    result = tenant.submit_issue(intake())
    class EmptyLeaseSource:
        # Explicit test double: this test integrates only the real Maintenance source.
        def period_summary(self, property_id, period):
            return LeaseRentPeriodData(rent=RentSectionData(availability=A.EMPTY),
                                       lease=LeaseSectionData(availability=A.EMPTY))
    collector = ReportDataCollector(MaintenanceReportSource(manager), EmptyLeaseSource())
    period = ReportPeriod(year=utcnow().year, month=utcnow().month)
    assert collector.collect("p1", period).maintenance.requests[0].request_id == result.request_id
    with store.transaction() as db:
        db.execute("DELETE FROM manager_property WHERE manager_id='m1'")
    with pytest.raises(PermissionError):
        collector.collect("p1", period)
