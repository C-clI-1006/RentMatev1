"""Regression guard for the integrated FastAPI entrypoint."""

from fastapi.testclient import TestClient

from main import create_app


def test_all_team_route_families_are_mounted_together(tmp_path):
    app = create_app(
        db_path=str(tmp_path / "integrated.db"),
        storage_root=str(tmp_path / "evidence"),
    )
    try:
        paths = set(app.openapi()["paths"])
        expected = {
            "/api/maintenance/requests",
            "/api/reviews",
            "/api/evidence/documents",
            "/api/evidence/maintenance-events/{event_id}",
            "/api/manager/monthly-reports/generate",
            "/api/ops/monthly-reports/run",
            "/api/ops/rent-alerts/run",
            "/api/ops/notifications/dispatch",
            "/api/notifications/inbox",
            "/api/communication/messages",
            "/api/ops/communication/maintenance-events/run",
        }
        assert expected <= paths, f"missing integrated routes: {sorted(expected - paths)}"
        with TestClient(app) as client:
            home = client.get("/", follow_redirects=False)
            assert home.status_code == 307
            assert home.headers["location"] == "/demo"
            page = client.get("/demo")
            assert page.status_code == 200
            assert "UC4/UC5 scheduled detection" in page.text
    finally:
        app.state.evidence_service._repo.close()
