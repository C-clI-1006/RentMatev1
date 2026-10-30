import json
from datetime import date, timedelta
from decimal import Decimal

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
