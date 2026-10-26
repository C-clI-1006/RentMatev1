"""Executable UC4/UC5 → review → delivery demonstration regression."""

import json
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from contracts.drafting import ReviewDecision
from lease_rent.service import run_due_alerts
from notification import SQLiteTenantInboxAdapter, dispatch_pending
from review.service import ReviewService
from scripts.seed_integrated_demo import seed
from shared import AuthContext
from shared.sqlite import init_connection


def test_seeded_demo_completes_approved_delivery_without_duplicates(tmp_path):
    database = tmp_path / "integrated-demo.db"
    seed(database)
    seed(database)  # launch script may safely be run again

    first = run_due_alerts(now=datetime.now(timezone.utc), db_path=database)
    assert first["rent_candidates"] == 1
    assert first["lease_candidates"] == 1
    assert first["drafts_created"] == 2

    second = run_due_alerts(now=datetime.now(timezone.utc), db_path=database)
    assert second["drafts_created"] == 0
    assert second["drafts_reused"] == 2

    db = init_connection(database)
    try:
        review = ReviewService(
            db,
            AuthContext("demo-tenant", "demo-property", "demo-manager"),
        )
        scheduled = [
            item
            for item in review.list_pending()
            if item.draft.source_kind in ("rent_reminder", "lease_deadline")
        ]
        assert {item.draft.source_kind for item in scheduled} == {
            "rent_reminder",
            "lease_deadline",
        }
        for item in scheduled:
            review.decide(
                item.draft.draft_id,
                expected_version=item.draft.version,
                decision=ReviewDecision.APPROVED,
            )

        results = dispatch_pending(db, SQLiteTenantInboxAdapter(db))
        assert len(results) == 2
        assert {result.status.value for result in results} == {"delivered"}
        assert db.execute("SELECT count(*) FROM tenant_inbox").fetchone()[0] == 2
        assert db.execute("SELECT count(*) FROM notification_event").fetchone()[0] == 2
    finally:
        db.close()


def test_demo_page_http_workflow_matches_the_documented_click_path(
    tmp_path, monkeypatch
):
    from main import create_app

    database = tmp_path / "http-demo.db"
    seed(database)
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "wrong-environment.db"))
    monkeypatch.setenv("RENTMATE_SCHEDULER_TOKEN", "scheduler-demo")
    monkeypatch.setenv("LLM_BASE_URL", "")
    monkeypatch.setenv("LLM_API_KEY", "")
    monkeypatch.setenv("LLM_MODEL", "")
    monkeypatch.setenv(
        "RENTMATE_AUTH_TOKENS",
        json.dumps(
            {
                "tenant-demo": {
                    "tenant_id": "demo-tenant",
                    "property_id": "demo-property",
                },
                "manager-demo": {
                    "tenant_id": "demo-tenant",
                    "property_id": "demo-property",
                    "manager_id": "demo-manager",
                },
            }
        ),
    )
    app = create_app(str(database), str(tmp_path / "evidence"))
    scheduler = {"Authorization": "Bearer scheduler-demo"}
    manager = {"Authorization": "Bearer manager-demo"}
    tenant = {"Authorization": "Bearer tenant-demo"}
    try:
        with TestClient(app) as client:
            # Every mounted module must use the database bound to this app,
            # never an unrelated process-level DATABASE_PATH.
            maintenance = client.get("/api/maintenance/requests", headers=tenant)
            assert maintenance.status_code == 200

            current = datetime.now(timezone.utc)
            report = client.post(
                "/api/ops/monthly-reports/run",
                headers=scheduler,
                json={
                    "property_id": "demo-property",
                    "year": current.year,
                    "month": current.month,
                },
            )
            assert report.status_code == 200
            report_history = client.get(
                "/api/manager/monthly-reports",
                headers=manager,
                params={
                    "property_id": "demo-property",
                    "year": current.year,
                    "month": current.month,
                },
            )
            assert report_history.status_code == 200
            assert len(report_history.json()) == 1

            run = client.post(
                "/api/ops/rent-alerts/run",
                params={"now": current.isoformat()},
                headers=scheduler,
            )
            assert run.status_code == 200
            assert run.json()["drafts_created"] == 2

            queue = client.get("/api/reviews", headers=manager)
            assert queue.status_code == 200
            scheduled = [
                item
                for item in queue.json()
                if item["draft"]["source_kind"]
                in ("rent_reminder", "lease_deadline")
            ]
            assert len(scheduled) == 2
            for item in scheduled:
                approved = client.post(
                    f"/api/reviews/{item['draft']['draft_id']}/decision",
                    headers=manager,
                    json={
                        "expected_version": item["draft"]["version"],
                        "decision": "approved",
                        "final_content": item["draft"]["content"],
                    },
                )
                assert approved.status_code == 200

            dispatch = client.post(
                "/api/ops/notifications/dispatch", headers=scheduler
            )
            assert dispatch.status_code == 200
            assert dispatch.json()["delivered"] == 2
            inbox = client.get("/api/notifications/inbox", headers=tenant)
            assert inbox.status_code == 200
            assert len(inbox.json()) == 2

            rerun = client.post(
                "/api/ops/rent-alerts/run",
                params={"now": datetime.now(timezone.utc).isoformat()},
                headers=scheduler,
            )
            assert rerun.status_code == 200
            assert rerun.json()["drafts_created"] == 0
    finally:
        app.state.evidence_service._repo.close()
