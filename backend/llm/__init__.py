"""LLM adapter (shared capability).

Single point for all model calls: config (base URL / model / key from
env), timeouts, structured-output validation, error conversion to
recognised failure results. Business modules never call the model
directly (guide Ch.19).

Until the school Qwen proxy is confirmed working, modules must use
labelled test doubles (stub classifiers/drafters) that keep the final
interface shapes. Do not present mocked inference as evidence of a
live agent.

This package exposes:
- `Assessment` / `AssessmentFailure`: the structured priority-assessment
  output (recommendation only — see contracts.maintenance).
- `IPriorityAssessor`: the interface business modules program against.
- `KeywordPriorityAssessor`: a LABELLED test double used in tests and
  until the proxy is available. It is deterministic keyword matching,
  not inference; its output is clearly labelled `source="stub"`.
- `LlmPriorityAssessor`: the live-adapter slot (fails closed to
  AssessmentFailure until the proxy is configured and confirmed).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from contracts.maintenance import Priority


@dataclass(frozen=True)
class Assessment:
    """A usable priority assessment (FR-MC-02).

    Always a *recommendation*: the maintenance state machine and the
    manager control what is persisted (guide 12.2).
    """

    priority: Priority
    rationale: str
    confidence: float  # uncertainty signal, NOT a calibrated guarantee
    emergency_indicator: bool  # True when emergency signs were detected
    provider_suggestion: str | None = None  # optional FR-MC-08
    source: str = "llm"  # "stub" for test doubles (labelled, guide 19)


@dataclass(frozen=True)
class AssessmentFailure:
    """Structured failure: missing information, conflict, or LLM error.

    A failure routes the request to UnderReview (UC3 A2); it is never
    silently turned into a default priority.
    """

    code: str  # "missing_info" | "conflict" | "llm_error"
    detail: str
    missing_fields: list[str] = field(default_factory=list)


class IPriorityAssessor(ABC):
    """Structured priority assessment behind the single LLM adapter."""

    @abstractmethod
    def assess(
        self,
        *,
        title: str,
        description: str,
        location: str | None,
        intake_emergency_flag: bool,
    ) -> Assessment | AssessmentFailure: ...
