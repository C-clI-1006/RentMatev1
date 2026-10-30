import json
from datetime import date
from decimal import Decimal

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
