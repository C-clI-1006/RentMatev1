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


def test_one_click_demo_covers_all_mounted_module_boundaries(tmp_path, monkeypatch):
    """Mirror the browser's full-system button without any live dependency."""

    from main import create_app

    database = tmp_path / "full-system-demo.db"
    seed(database)
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
                "manager-other-demo": {
                    "tenant_id": "demo-tenant-2",
                    "property_id": "demo-property-2",
                    "manager_id": "demo-manager-2",
                },
            }
        ),
    )
    app = create_app(str(database), str(tmp_path / "evidence"))
    scheduler = {"Authorization": "Bearer scheduler-demo"}
    manager = {"Authorization": "Bearer manager-demo"}
    tenant = {"Authorization": "Bearer tenant-demo"}
    other_manager = {"Authorization": "Bearer manager-other-demo"}
    try:
        with TestClient(app) as client:
            assert client.get("/health").json() == {"status": "ok"}

            upload = client.post(
                "/api/evidence/documents",
                headers=tenant,
                data={"property_id": "demo-property"},
                files={"file": ("completion.txt", b"Synthetic completion evidence", "text/plain")},
            )
            assert upload.status_code == 200
            assert upload.json()["status"] == "accepted"
            document_id = upload.json()["document"]["document_id"]

            intake = client.post(
                "/api/maintenance/requests",
                headers={**tenant, "Idempotency-Key": "full-demo-test"},
                json={
                    "title": "Loose kitchen cupboard handle",
                    "description": "Loose but usable. No injury or immediate hazard.",
                    "location": "Kitchen",
                    "evidence_refs": [document_id],
                },
            )
            assert intake.status_code == 200
            request_id = intake.json()["request_id"]
            record = client.get(
                f"/api/maintenance/requests/{request_id}", headers=manager
            ).json()
            if record["status"] == "under_review":
                assessed = client.post(
                    f"/api/maintenance/requests/{request_id}/assessment",
                    headers=manager,
                    json={
                        "expected_version": record["version"],
                        "priority": "routine",
                        "rationale": "Deterministic demo fallback",
                    },
                )
                assert assessed.status_code == 200
                record = client.get(
                    f"/api/maintenance/requests/{request_id}", headers=manager
                ).json()
            for target in ("approved", "in_progress", "awaiting_confirmation", "closed"):
                changed = client.post(
                    f"/api/maintenance/requests/{request_id}/status",
                    headers=manager,
                    json={
                        "expected_version": record["version"],
                        "target_status": target,
                        "reason": f"Full demo transition to {target}",
                        "evidence_refs": [document_id],
                    },
                )
                assert changed.status_code == 200, changed.text
                record = client.get(
                    f"/api/maintenance/requests/{request_id}", headers=manager
                ).json()
            assert record["status"] == "closed"

            validated = client.post(
                "/api/evidence/references/validate",
                headers=tenant,
                json={
                    "property_id": "demo-property",
                    "evidence_refs": [document_id],
                },
            )
            assert validated.status_code == 200

            communication = client.post(
                "/api/communication/messages",
                headers=tenant,
                json={"content": "When does my current lease end?"},
            )
            assert communication.status_code == 200
            assert communication.json()["delivery_status"] == "not_dispatched"

            handoff = client.post(
                "/api/ops/communication/maintenance-events/run", headers=scheduler
            )
            assert handoff.status_code == 200

            current = datetime.now(timezone.utc)
            first = client.post(
                "/api/ops/rent-alerts/run",
                headers=scheduler,
                params={"now": current.isoformat()},
            )
            second = client.post(
                "/api/ops/rent-alerts/run",
                headers=scheduler,
                params={"now": current.isoformat()},
            )
            assert first.status_code == second.status_code == 200
            assert second.json()["drafts_created"] == 0

            denied = client.get("/api/reviews/demo-review-draft", headers=other_manager)
            assert denied.status_code == 404

            queue = client.get("/api/reviews", headers=manager).json()
            rent = next(item for item in queue if item["draft"]["source_kind"] == "rent_reminder")
            lease = next(item for item in queue if item["draft"]["source_kind"] == "lease_deadline")
            approved = client.post(
                f"/api/reviews/{rent['draft']['draft_id']}/decision",
                headers=manager,
                json={
                    "expected_version": rent["draft"]["version"],
                    "decision": "approved",
                    "final_content": rent["draft"]["content"],
                },
            )
            rejected = client.post(
                f"/api/reviews/{lease['draft']['draft_id']}/decision",
                headers=manager,
                json={
                    "expected_version": lease["draft"]["version"],
                    "decision": "rejected",
                    "reason": "Synthetic full-demo rejection",
                },
            )
            assert approved.status_code == rejected.status_code == 200

            dispatched = client.post(
                "/api/ops/notifications/dispatch", headers=scheduler
            )
            assert dispatched.status_code == 200
            inbox = client.get("/api/notifications/inbox", headers=tenant).json()
            assert any(
                item["event_id"] == f"draft:{rent['draft']['draft_id']}:v1"
                for item in inbox
            )

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
            history = client.get(
                "/api/manager/monthly-reports",
                headers=manager,
                params={
                    "property_id": "demo-property",
                    "year": current.year,
                    "month": current.month,
                },
            )
            assert history.status_code == 200
            assert history.json()

            summaries = client.get("/api/maintenance/summaries", headers=manager)
            documents = client.get(
                "/api/evidence/documents",
                headers=tenant,
                params={"property_id": "demo-property"},
            )
            assert any(item["request_id"] == request_id for item in summaries.json())
            assert any(item["document_id"] == document_id for item in documents.json())
    finally:
        app.state.evidence_service._repo.close()
