"""Manager-initiated HTTP generation of the monthly report.

The manager identity is resolved server-side from RENTMATE_AUTH_TOKENS
+ AuthContext; the request body carries only property/period/idempotency
key. A labelled test double stands in for the LLM narrator (no model
call); the data providers stay real against a temp SQLite database.
"""

import json
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from contracts.maintenance import MaintenanceStatus, MaintenanceView, Priority
from contracts.reporting import NarrativeSource, ReportNarrative
from maintenance.store import Store
from reporting.manager_api import get_connection as manager_get_connection
from shared.sqlite import init_connection

TS = "2026-10-01T00:00:00Z"
URL = "/api/manager/monthly-reports/generate"
LIST_URL = "/api/manager/monthly-reports"
NARRATIVE_MARKER = "STUB narrative for manager HTTP test"


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


def _fake_narrator(counter):
    def build():
        def narrate(_data):
            counter["calls"] += 1
            return ReportNarrative(
                source=NarrativeSource.STUB,
                text=NARRATIVE_MARKER,
                generated_at=datetime.now(timezone.utc),
            )

        return narrate

    return build


def _client(tmp_path, monkeypatch):
    path = tmp_path / "manager_api.db"
    _seed_real_sources(path)

    def api_connection():
        value = init_connection(path)
        try:
            yield value
        finally:
            value.close()

    from main import app
    from reporting import manager_api

    app.dependency_overrides[manager_get_connection] = api_connection
    monkeypatch.setenv(
        "RENTMATE_AUTH_TOKENS",
        json.dumps({"manager-token": {"tenant_id": "t1", "property_id": "p1", "manager_id": "m1"}}),
    )
    # Fail-closed guard: even if a seam were missed, the live narrator
    # must not be able to call a model from the test environment.
    monkeypatch.setenv("LLM_BASE_URL", "")
    monkeypatch.setenv("LLM_MODEL", "")
    monkeypatch.setenv("LLM_API_KEY", "")
    counter = {"calls": 0}
    monkeypatch.setattr(manager_api, "build_narrator", _fake_narrator(counter))
    return TestClient(app), counter


def _post(client, token, body):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return client.post(URL, headers=headers, json=body)


def _clear_overrides():
    from main import app

    app.dependency_overrides.clear()


def test_manager_http_generates_report_from_server_side_identity(tmp_path, monkeypatch):
    client, counter = _client(tmp_path, monkeypatch)
    try:
        body = {"property_id": "p1", "year": 2026, "month": 10, "idempotency_key": "k1"}
        # No token or an unrecognised token is rejected before anything else.
        assert _post(client, None, body).status_code == 401
        assert _post(client, "wrong", body).status_code == 401

        response = _post(client, "manager-token", body)
        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "generated"
        assert payload["reused"] is False
        report = payload["report"]
        # Identity comes from the auth context, not the request body.
        assert report["created_by"] == "manager:m1"
        assert report["trigger"]["manager_id"] == "m1"
        assert report["trigger"]["trigger"] == "manager"
        assert report["event_id"] == "k1"
        assert report["generation"] == 1
        assert report["property_id"] == "p1"
        assert report["period"] == {"year": 2026, "month": 10}
        assert report["outcome"] == "complete"
        assert {risk["rule_id"] for risk in report["risks"]["risks"]} == {
            "risk.open_emergency",
            "risk.overdue_rent",
            "risk.lease_expiring",
        }
        narrative = report["narrative"]
        assert narrative["source"] == "stub"
        assert narrative["text"] == NARRATIVE_MARKER
        assert counter["calls"] == 1

        # A manager_id in the body is not an accepted field (422), so it
        # can never be mistaken for the trusted identity.
        spoofed = dict(body, manager_id="m999")
        assert _post(client, "manager-token", spoofed).status_code == 422
    finally:
        _clear_overrides()


def test_manager_http_denies_unbound_property(tmp_path, monkeypatch):
    client, counter = _client(tmp_path, monkeypatch)
    try:
        body = {"property_id": "p2", "year": 2026, "month": 10, "idempotency_key": "k1"}
        response = _post(client, "manager-token", body)
        assert response.status_code == 403
        # The attempt is audited only: no report row, no narrator call.
        assert counter["calls"] == 0
        connection = init_connection(tmp_path / "manager_api.db")
        try:
            assert connection.execute("SELECT COUNT(*) FROM report").fetchone()[0] == 0
            assert (
                connection.execute(
                    "SELECT COUNT(*) FROM audit WHERE action='report.failed'"
                ).fetchone()[0]
                == 1
            )
        finally:
            connection.close()
    finally:
        _clear_overrides()


def test_manager_http_same_idempotency_key_reuses_report(tmp_path, monkeypatch):
    client, counter = _client(tmp_path, monkeypatch)
    try:
        body = {"property_id": "p1", "year": 2026, "month": 10, "idempotency_key": "k1"}
        first = _post(client, "manager-token", body)
        assert first.status_code == 200
        assert first.json()["reused"] is False
        assert counter["calls"] == 1

        second = _post(client, "manager-token", body)
        assert second.status_code == 200
        payload = second.json()
        assert payload["reused"] is True
        assert payload["report"]["report_id"] == first.json()["report"]["report_id"]
        assert payload["report"]["generation"] == 1
        assert counter["calls"] == 1
    finally:
        _clear_overrides()


def test_manager_http_new_idempotency_key_creates_next_generation(tmp_path, monkeypatch):
    client, counter = _client(tmp_path, monkeypatch)
    try:
        base = {"property_id": "p1", "year": 2026, "month": 10}
        first = _post(client, "manager-token", dict(base, idempotency_key="k1"))
        assert first.status_code == 200
        first_report = first.json()["report"]

        second = _post(client, "manager-token", dict(base, idempotency_key="k2"))
        assert second.status_code == 200
        payload = second.json()
        report = payload["report"]
        assert payload["reused"] is False
        assert report["generation"] == first_report["generation"] + 1
        assert report["event_id"] == "k2"
        assert report["report_id"] != first_report["report_id"]
        assert counter["calls"] == 2
    finally:
        _clear_overrides()


def _get(client, token, url):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return client.get(url, headers=headers)


def _report_count(db_path):
    connection = init_connection(db_path)
    try:
        return connection.execute("SELECT COUNT(*) FROM report").fetchone()[0]
    finally:
        connection.close()


def test_manager_http_fetch_single_report_is_read_only(tmp_path, monkeypatch):
    client, counter = _client(tmp_path, monkeypatch)
    try:
        body = {"property_id": "p1", "year": 2026, "month": 10, "idempotency_key": "k1"}
        assert _post(client, "manager-token", body).status_code == 200
        report_id = _post(client, "manager-token", body).json()["report"]["report_id"]

        # No token / wrong token is rejected before anything is read.
        assert _get(client, None, f"{LIST_URL}/{report_id}").status_code == 401
        assert _get(client, "wrong", f"{LIST_URL}/{report_id}").status_code == 401

        response = _get(client, "manager-token", f"{LIST_URL}/{report_id}")
        assert response.status_code == 200
        report = response.json()
        assert report["report_id"] == report_id
        assert report["property_id"] == "p1"
        assert report["period"] == {"year": 2026, "month": 10}
        assert report["created_by"] == "manager:m1"
        # Reading must not collect, call the LLM or write a new report.
        assert counter["calls"] == 1
        assert _report_count(tmp_path / "manager_api.db") == 1
    finally:
        _clear_overrides()


def test_manager_http_lists_versions_in_generation_order(tmp_path, monkeypatch):
    client, counter = _client(tmp_path, monkeypatch)
    try:
        base = {"property_id": "p1", "year": 2026, "month": 10}
        first = _post(client, "manager-token", dict(base, idempotency_key="k1")).json()["report"]
        second = _post(client, "manager-token", dict(base, idempotency_key="k2")).json()["report"]

        response = _get(
            client, "manager-token", f"{LIST_URL}?property_id=p1&year=2026&month=10"
        )
        assert response.status_code == 200
        reports = response.json()
        assert [item["report_id"] for item in reports] == [
            first["report_id"],
            second["report_id"],
        ]
        assert [item["generation"] for item in reports] == [1, 2]
        # Reading must not collect, call the LLM or write a new report.
        assert counter["calls"] == 2
        assert _report_count(tmp_path / "manager_api.db") == 2
    finally:
        _clear_overrides()


def test_manager_http_list_empty_period_returns_empty_list(tmp_path, monkeypatch):
    client, counter = _client(tmp_path, monkeypatch)
    try:
        # No report has been generated for 2026-09.
        response = _get(
            client, "manager-token", f"{LIST_URL}?property_id=p1&year=2026&month=9"
        )
        assert response.status_code == 200
        assert response.json() == []
        assert counter["calls"] == 0
        assert _report_count(tmp_path / "manager_api.db") == 0
    finally:
        _clear_overrides()


def test_manager_http_get_report_not_found(tmp_path, monkeypatch):
    client, counter = _client(tmp_path, monkeypatch)
    try:
        body = {"property_id": "p1", "year": 2026, "month": 10, "idempotency_key": "k1"}
        assert _post(client, "manager-token", body).status_code == 200

        response = _get(client, "manager-token", f"{LIST_URL}/no-such-report")
        assert response.status_code == 404
        assert response.json()["detail"] == "Report not found"
        assert counter["calls"] == 1
        assert _report_count(tmp_path / "manager_api.db") == 1
    finally:
        _clear_overrides()


def test_manager_http_get_denies_unbound_property_without_leaking_existence(
    tmp_path, monkeypatch
):
    client, counter = _client(tmp_path, monkeypatch)
    try:
        body = {"property_id": "p1", "year": 2026, "month": 10, "idempotency_key": "k1"}
        generated = _post(client, "manager-token", body)
        assert generated.status_code == 200
        report_id = generated.json()["report"]["report_id"]

        # A manager with no binding for the requested property is denied.
        # The answer must be IDENTICAL whether or not the report exists,
        # so the denial never leaks report existence.
        bound = _get(client, "manager-token", f"{LIST_URL}/{report_id}")
        assert bound.status_code == 200

        connection = init_connection(tmp_path / "manager_api.db")
        try:
            # m2 is a valid manager for p2 (so its token passes auth),
            # but has NO binding for p1.
            connection.executescript(
                f"""
                INSERT INTO property (property_id,name,created_at)
                  VALUES ('p2','Second','{TS}');
                INSERT INTO manager (manager_id,name,created_at)
                  VALUES ('m2','Other Manager','{TS}');
                INSERT INTO manager_property (manager_id,property_id,created_at)
                  VALUES ('m2','p2','{TS}');
                """
            )
            connection.commit()
        finally:
            connection.close()
        monkeypatch.setenv(
            "RENTMATE_AUTH_TOKENS",
            json.dumps(
                {
                    "manager-token": {
                        "tenant_id": "t1",
                        "property_id": "p1",
                        "manager_id": "m1",
                    },
                    "m2-token": {
                        "tenant_id": "t1",
                        "property_id": "p2",
                        "manager_id": "m2",
                    },
                }
            ),
        )

        # Unbound manager: 404 for the report that exists on p1...
        denied_existing = _get(client, "m2-token", f"{LIST_URL}/{report_id}")
        # ...and the same 404 for a report that does not exist.
        denied_absent = _get(client, "m2-token", f"{LIST_URL}/no-such-report")
        assert denied_existing.status_code == 404
        assert denied_existing.json()["detail"] == "Report not found"
        assert denied_absent.status_code == 404
        assert denied_absent.json()["detail"] == "Report not found"
        # The list endpoint leaks nothing either (p1 is bound to m1 only).
        denied_list = _get(
            client, "m2-token", f"{LIST_URL}?property_id=p1&year=2026&month=10"
        )
        assert denied_list.status_code == 404
        assert denied_list.json()["detail"] == "Report not found"
        # m2's own (bound) property still answers normally: no report yet.
        own_list = _get(
            client, "m2-token", f"{LIST_URL}?property_id=p2&year=2026&month=10"
        )
        assert own_list.status_code == 200
        assert own_list.json() == []
        # No collection, no LLM, no writes from any of the denied reads.
        assert counter["calls"] == 1
        assert _report_count(tmp_path / "manager_api.db") == 1
    finally:
        _clear_overrides()


def test_manager_http_get_denies_revoked_permission(tmp_path, monkeypatch):
    client, counter = _client(tmp_path, monkeypatch)
    try:
        body = {"property_id": "p1", "year": 2026, "month": 10, "idempotency_key": "k1"}
        generated = _post(client, "manager-token", body)
        assert generated.status_code == 200
        report_id = generated.json()["report"]["report_id"]

        # The binding worked before the permission was revoked.
        assert _get(client, "manager-token", f"{LIST_URL}/{report_id}").status_code == 200

        connection = init_connection(tmp_path / "manager_api.db")
        try:
            connection.execute(
                "DELETE FROM manager_property WHERE manager_id='m1'"
            )
            connection.commit()
        finally:
            connection.close()

        # After revocation the auth gate (token -> its own property
        # binding) denies before any report read. The denial must be
        # IDENTICAL whether or not the report exists, so it never leaks
        # report existence.
        denied_list = _get(
            client, "manager-token", f"{LIST_URL}?property_id=p1&year=2026&month=10"
        )
        assert denied_list.status_code == 403
        denied_single = _get(client, "manager-token", f"{LIST_URL}/{report_id}")
        assert denied_single.status_code == 403
        assert denied_single.json()["detail"] == denied_list.json()["detail"]
        # A nonexistent id is denied in exactly the same way.
        denied_absent = _get(client, "manager-token", f"{LIST_URL}/no-such-report")
        assert denied_absent.status_code == 403
        assert denied_absent.json()["detail"] == denied_single.json()["detail"]
        assert counter["calls"] == 1
        assert _report_count(tmp_path / "manager_api.db") == 1
    finally:
        _clear_overrides()
