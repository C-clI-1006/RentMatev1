"""Bounded Qwen reasoning for already-verified lease/rent risks.

The advisor is deliberately separate from the deterministic rules: Qwen may
recommend a next step, but it cannot calculate authoritative balances or
perform an action. Callers must persist/execute the validated recommendation
through the existing review workflow.
"""

from __future__ import annotations

import json
import math
import os
from enum import Enum
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field

from .models import LeaseRentRisk
from llm.configuration import get_configuration


class RiskAction(str, Enum):
    NO_ACTION = "NO_ACTION"
    PREPARE_REMINDER = "PREPARE_REMINDER"
    REQUEST_PAYMENT_VERIFICATION = "REQUEST_PAYMENT_VERIFICATION"
    REQUEST_MANAGER_REVIEW = "REQUEST_MANAGER_REVIEW"
    PREPARE_LEASE_EXPIRY_NOTICE = "PREPARE_LEASE_EXPIRY_NOTICE"


class RiskRecommendation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    risk_category: str = Field(min_length=1, max_length=100)
    risk_level: str = Field(min_length=1, max_length=20)
    explanation: str = Field(min_length=1, max_length=2000)
    recommended_action: RiskAction
    supporting_fact_references: list[str] = Field(min_length=1, max_length=20)
    requires_manager_review: bool
    uncertainty_reason: str | None = Field(default=None, max_length=500)


class RiskAssessmentResult(BaseModel):
    recommendation: RiskRecommendation | None = None
    source: str
    failure_code: str | None = None


class LeaseRentRiskAdvisor:
    """Ask the configured model for a bounded recommendation.

    The payload is composed only from caller-supplied verified facts. A
    missing configuration, transport failure, or invalid model response is a
    visible fallback and never an invented AI decision.
    """

    def __init__(self, *, timeout: float | None = None) -> None:
        self._timeout = timeout

    def assess(
        self,
        risk: LeaseRentRisk,
        *,
        verified_facts: list[dict[str, Any]],
        reminder_status: str | None = None,
    ) -> RiskAssessmentResult:
        config = get_configuration()
        if not all((config.base_url, config.api_key, config.model)):
            return self._fallback("not_configured")
        try:
            timeout = self._timeout or float(os.getenv("LLM_TIMEOUT_SECONDS", "30"))
            if not math.isfinite(timeout) or timeout <= 0:
                return self._fallback("configuration")
        except (TypeError, ValueError):
            return self._fallback("configuration")
        payload = {
            "risk": {
                "rule_id": risk.rule_id,
                "severity": risk.severity.value,
                "source_type": risk.source_type,
                "source_id": risk.source_id,
            },
            "verified_facts": verified_facts,
            "reminder_status": reminder_status,
        }
        body = {
            "model": config.model,
            "messages": [
                {"role": "system", "content": (
                    "You are a lease and rent risk advisor. Use only supplied "
                    "verified facts. Do not recalculate or change authoritative "
                    "values. Return JSON only with fields risk_category, "
                    "risk_level, explanation, recommended_action, "
                    "supporting_fact_references, requires_manager_review, "
                    "uncertainty_reason. Never approve, modify records, or send "
                    "notifications."
                )},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            "reasoning_effort": "none",
            "stream": False,
            "max_completion_tokens": 800,
        }
        try:
            with httpx.Client(timeout=timeout, trust_env=False) as client:
                response = client.post(
                    config.base_url.rstrip("/") + "/chat/completions",
                    headers={"Authorization": "Bearer " + config.api_key},
                    json=body,
                )
            if response.is_error:
                return self._fallback("http_rejected")
            message = response.json()["choices"][0]["message"]
            content = message.get("content")
            if isinstance(content, list):
                content = "".join(
                    part.get("text", "") if isinstance(part, dict) else str(part)
                    for part in content
                )
            if not isinstance(content, str) or not content.strip():
                return self._fallback("invalid_response")
            text = content.strip()
            lines = text.splitlines()
            if (len(lines) >= 3 and lines[0].strip().lower() in ("```", "```json")
                    and lines[-1].strip() == "```"):
                text = "\n".join(lines[1:-1])
            try:
                decoded = json.loads(text)
            except json.JSONDecodeError:
                start, end = text.find("{"), text.rfind("}")
                if start < 0 or end <= start:
                    return self._fallback("invalid_response")
                decoded = json.loads(text[start:end + 1])
            recommendation = RiskRecommendation.model_validate(decoded)
            if not set(recommendation.supporting_fact_references).issubset(
                {str(item.get("source_id")) for item in verified_facts}
            ):
                return self._fallback("unverified_reference")
            if not recommendation.requires_manager_review:
                recommendation = recommendation.model_copy(update={"requires_manager_review": True})
            return RiskAssessmentResult(source="llm", recommendation=recommendation)
        except httpx.HTTPError:
            return self._fallback("unavailable")
        except (ValueError, KeyError, IndexError, TypeError):
            return self._fallback("invalid_response")

    @staticmethod
    def _fallback(code: str) -> RiskAssessmentResult:
        return RiskAssessmentResult(source="fallback", failure_code=code)


def validate_action(risk: LeaseRentRisk, recommendation: RiskRecommendation) -> bool:
    """Validate a model action against deterministic risk type and safeguards."""

    if not recommendation.requires_manager_review:
        return False
    if risk.rule_id == "risk.overdue_rent":
        return recommendation.recommended_action in {
            RiskAction.NO_ACTION,
            RiskAction.PREPARE_REMINDER,
            RiskAction.REQUEST_PAYMENT_VERIFICATION,
            RiskAction.REQUEST_MANAGER_REVIEW,
        }
    if risk.rule_id == "risk.lease_expiring":
        return recommendation.recommended_action in {
            RiskAction.NO_ACTION,
            RiskAction.PREPARE_LEASE_EXPIRY_NOTICE,
            RiskAction.REQUEST_MANAGER_REVIEW,
        }
    return False
