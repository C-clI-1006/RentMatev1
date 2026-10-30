import json
from datetime import date, timedelta
from decimal import Decimal
from contextlib import closing

import pytest

from fastapi.testclient import TestClient

from lease_rent import (
    LeaseRentRiskAdvisor,
    LeaseRentRisk,
    RiskAction,
    RiskSeverity,
    RiskRecommendation,
    RentRecordInput,
    assess_rent_risk,
    validate_action,
)
from llm.configuration import ModelConfiguration, set_configuration
from main import create_app
from scripts.seed_integrated_demo import seed
from shared.sqlite import init_connection
from lease_rent.risk_advisor import RiskAssessmentResult


@pytest.mark.parametrize("start,end", [
    ("2026-01-01", "2020-01-01"),
    ("invalid-date", "2026-12-31"),
    ("2026-01-01", "invalid-date"),
    ("", "2026-12-31"),
    ("2026-01-01", ""),
])
def test_risk_endpoint_isolates_invalid_lease_facts(tmp_path, monkeypatch, start, end):
    database = tmp_path / "lease-risk.db"
    seed(database)
    with closing(init_connection(database)) as db:
        db.execute(
            "INSERT INTO lease(lease_id,property_id,tenant_id,start_date,end_date,created_at) "
            "VALUES ('bad-lease','demo-property','demo-tenant',?,?,?)",
            (start, end, "2026-10-30T00:00:00Z"),
        )
        db.commit()
        original = [tuple(row) for row in db.execute("SELECT * FROM lease ORDER BY lease_id")]
    calls = []

    def advise(self, risk, **kwargs):
        calls.append(risk.source_id)
        return RiskAssessmentResult(source="fallback", failure_code="explicit_test_stub")

    monkeypatch.setattr(LeaseRentRiskAdvisor, "assess", advise)
    monkeypatch.setenv("RENTMATE_SCHEDULER_TOKEN", "scheduler-fixture")
    app = create_app(str(database), str(tmp_path / "uploads"))
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            assert client.post("/api/ops/lease-rent/risk-assessment").status_code == 401
            response = client.post(
                "/api/ops/lease-rent/risk-assessment",
                headers={"Authorization": "Bearer scheduler-fixture"},
            )
        assert response.status_code == 200
        results = response.json()["results"]
        invalid = next(item for item in results if item.get("source", {}).get("source_id") == "bad-lease")
        assert invalid == {
            "risk": None,
            "source": {"source_type": "lease", "source_id": "bad-lease"},
            "assessment": {"recommendation": None, "source": "fallback", "failure_code": "incomplete_authoritative_facts"},
            "requires_manager_review": True,
        }
        assert {item["risk"]["source_id"] for item in results if item.get("risk")} == {"demo-rent", "demo-lease"}
        assert set(calls) == {"demo-rent", "demo-lease"}  # Bad facts never reach inference.
        with closing(init_connection(database)) as db:
            assert [tuple(row) for row in db.execute("SELECT * FROM lease ORDER BY lease_id")] == original
    finally:
        app.state.evidence_service._repo.close()


def test_risk_endpoint_keeps_no_deadline_and_inactive_lease_semantics(tmp_path, monkeypatch):
    database = tmp_path / "no-deadline.db"
    seed(database)
    today = date.today()
    with closing(init_connection(database)) as db:
        for lid, start, end in [
            ("open-ended", today.isoformat(), None),
            ("expired", (today - timedelta(days=20)).isoformat(), (today - timedelta(days=1)).isoformat()),
            ("future", (today + timedelta(days=1)).isoformat(), (today + timedelta(days=15)).isoformat()),
        ]:
            db.execute("INSERT INTO lease(lease_id,property_id,tenant_id,start_date,end_date,created_at) "
                       "VALUES (?,'demo-property','demo-tenant',?,?,'2026-10-30T00:00:00Z')", (lid, start, end))
        db.commit()
    monkeypatch.setenv("RENTMATE_SCHEDULER_TOKEN", "scheduler-fixture")
    monkeypatch.setattr(LeaseRentRiskAdvisor, "assess", lambda *args, **kwargs:
                        RiskAssessmentResult(source="fallback", failure_code="explicit_test_stub"))
    app = create_app(str(database), str(tmp_path / "uploads"))
    try:
        with TestClient(app) as client:
            response = client.post("/api/ops/lease-rent/risk-assessment", headers={"Authorization": "Bearer scheduler-fixture"})
        assert response.status_code == 200
        assert {item["risk"]["source_id"] for item in response.json()["results"]} == {"demo-rent", "demo-lease"}
    finally:
        app.state.evidence_service._repo.close()


class _Response:
    is_error = False

    def __init__(self, content):
        self._content = content

    def json(self):
        return {"choices": [{"message": {"content": json.dumps(self._content)}}]}


def test_advisor_validates_qwen_recommendation(monkeypatch):
    set_configuration(ModelConfiguration("https://example.test/v1", "qwen", "secret"))
    monkeypatch.setattr(
        "lease_rent.risk_advisor.httpx.Client.post",
        lambda *args, **kwargs: _Response({
            "risk_category": "overdue_rent",
            "risk_level": "high",
            "explanation": "Payment remains outstanding.",
            "recommended_action": "REQUEST_PAYMENT_VERIFICATION",
            "supporting_fact_references": ["rent-1"],
            "requires_manager_review": True,
            "uncertainty_reason": None,
        }),
    )
    risk = assess_rent_risk(
        RentRecordInput("rent-1", date(2026, 10, 1), Decimal("100"), Decimal("0")),
        as_of=date(2026, 10, 20),
    )
    result = LeaseRentRiskAdvisor(timeout=1).assess(
        risk, verified_facts=[{"source_id": "rent-1", "status": "overdue"}]
    )
    assert result.source == "llm"
    assert result.recommendation.recommended_action is RiskAction.REQUEST_PAYMENT_VERIFICATION
    assert validate_action(risk, result.recommendation)
    set_configuration(None)


def test_advisor_rejects_unverified_references(monkeypatch):
    set_configuration(ModelConfiguration("https://example.test/v1", "qwen", "secret"))
    monkeypatch.setattr(
        "lease_rent.risk_advisor.httpx.Client.post",
        lambda *args, **kwargs: _Response({
            "risk_category": "overdue_rent", "risk_level": "high",
            "explanation": "x", "recommended_action": "NO_ACTION",
            "supporting_fact_references": ["invented"],
            "requires_manager_review": True, "uncertainty_reason": None,
        }),
    )
    risk = LeaseRentRisk("risk.overdue_rent", RiskSeverity.HIGH, "rent_record", "rent-1", "x")
    result = LeaseRentRiskAdvisor(timeout=1).assess(risk, verified_facts=[])
    assert result.source == "fallback"
    assert result.failure_code == "unverified_reference"
    set_configuration(None)


def test_advisor_rejects_action_incompatible_with_deterministic_risk(monkeypatch):
    set_configuration(ModelConfiguration("https://example.test/v1", "qwen", "secret"))
    monkeypatch.setattr(
        "lease_rent.risk_advisor.httpx.Client.post",
        lambda *args, **kwargs: _Response({
            "risk_category": "overdue_rent", "risk_level": "high",
            "explanation": "x", "recommended_action": "PREPARE_LEASE_EXPIRY_NOTICE",
            "supporting_fact_references": ["rent-1"],
            "requires_manager_review": True, "uncertainty_reason": None,
        }),
    )
    risk = LeaseRentRisk(
        "risk.overdue_rent", RiskSeverity.HIGH, "rent_record", "rent-1", "x"
    )
    result = LeaseRentRiskAdvisor(timeout=1).assess(
        risk, verified_facts=[{"source_id": "rent-1"}]
    )
    assert result.source == "fallback"
    assert result.failure_code == "invalid_action"
    assert result.recommendation is None
    set_configuration(None)


def test_risk_endpoint_isolates_incomplete_amounts_and_keeps_valid_results(
    tmp_path, monkeypatch
):
    database = tmp_path / "risk.db"
    seed(database)
    db = init_connection(database)
    try:
        db.execute(
            "INSERT INTO rent_record "
            "(rent_record_id,lease_id,property_id,period,due_date,amount_due,"
            "amount_paid,status,created_at) VALUES (?,?,?,?,?,NULL,?,?,?)",
            (
                "rent-missing-amount", "demo-lease", "demo-property",
                date.today().strftime("%Y-%m"),
                (date.today() - timedelta(days=3)).isoformat(),
                0, "overdue", "2026-10-30T00:00:00Z",
            ),
        )
        db.commit()
    finally:
        db.close()
    monkeypatch.setenv("RENTMATE_SCHEDULER_TOKEN", "scheduler-test")
    set_configuration(None)

    with TestClient(create_app(str(database), str(tmp_path / "evidence"))) as client:
        response = client.post(
            "/api/ops/lease-rent/risk-assessment",
            headers={"Authorization": "Bearer scheduler-test"},
        )

    assert response.status_code == 200
    results = response.json()["results"]
    incomplete = next(
        item for item in results
        if item.get("source", {}).get("source_id") == "rent-missing-amount"
    )
    assert incomplete["requires_manager_review"] is True
    assert incomplete["assessment"]["failure_code"] == "incomplete_authoritative_facts"
    assert any(
        (item.get("risk") or {}).get("source_id") == "demo-rent"
        for item in results
    )
