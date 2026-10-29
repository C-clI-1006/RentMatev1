"""Message classification port + labelled test double (guide 19.x, 13.x).

Classification is an LLM task behind this port. Until the school Qwen
proxy is confirmed working, `KeywordClassifier` is a LABELLED TEST
DOUBLE: deterministic keyword heuristics, no model call. It keeps the
final interface shape so the real model-backed classifier can be
swapped in without touching the communication module.

Validation rule (binding): an LLM output that does not match
`allowed` (or is not a string) is NOT "unknown" — it is invalid, and
the port returns None so the caller routes to human handoff.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
import json
import os
import re

import httpx
from pydantic import BaseModel, ConfigDict, Field
from .configuration import get_configuration


class Category(str, Enum):
    MAINTENANCE = "maintenance"
    LEASE_RENT = "lease_rent"
    SMALLTALK = "smalltalk"
    UNKNOWN = "unknown"


ALLOWED_CATEGORIES = frozenset(c.value for c in Category)


@dataclass(frozen=True)
class Classification:
    """Validated classification outcome. `confidence` is informational;
    routing must never depend on it alone."""

    category: Category
    confidence: float = 1.0
    note: str | None = None


class BaseClassifier(ABC):
    """Port: returns a validated classification, or None when the raw
    model output was invalid / ambiguous -> human handoff (binding)."""

    @abstractmethod
    def classify(self, content: str) -> Classification | None: ...


class KeywordClassifier(BaseClassifier):
    """Labelled test double — deterministic, no model call.

    Order matters: lease/rent signals are checked before maintenance
    so "can I pay rent online" routes to lease_rent.
    """

    MAINTENANCE_KEYWORDS = (
        "leak", "leaking", "leaky", "broken", "not working", "no hot water", "no water",
        "no heat", "no power", "plumbing", "electric", "flood",
        "mould", "mold", "repair", "fix it", "door won", "window won",
        "appliance", "stove", "oven", "fridge", "toilet", "heater",
        "cupboard handle", "cabinet handle", "handle is loose", "handle loose",
        "loose handle", "tap dripping", "tap is dripping", "dripping tap",
        "faucet dripping", "faucet is dripping", "dripping faucet",
        "air conditioner", "air conditioning", "stopped working",
        "gas smell", "smell of gas", "smell gas", "sparking outlet",
    )
    LEASE_RENT_KEYWORDS = (
        "rent", "lease", "payment", "paid", "invoice", "bill",
        "term", "clause", "deposit", "due date", "renew", "move out",
        "moving out",
    )
    SMALLTALK_KEYWORDS = ("hi", "hello", "thanks", "thank you", "good morning")

    def classify(self, content: str) -> Classification | None:
        lowered = " ".join(content.casefold().split())
        for kw in self.LEASE_RENT_KEYWORDS:
            if self._contains_phrase(lowered, kw):
                return Classification(Category.LEASE_RENT, 0.8, "keyword:lease_rent")
        for kw in self.MAINTENANCE_KEYWORDS:
            if self._contains_phrase(lowered, kw):
                return Classification(Category.MAINTENANCE, 0.8, "keyword:maintenance")
        for kw in self.SMALLTALK_KEYWORDS:
            if self._contains_phrase(lowered, kw):
                return Classification(Category.SMALLTALK, 0.9, "keyword:smalltalk")
        # No signal at all -> ambiguous: let the service hand off.
        return Classification(Category.UNKNOWN, 0.2, "no keyword signal")

    @staticmethod
    def _contains_phrase(text: str, phrase: str) -> bool:
        """Match whole words without pretending this heuristic is an LLM."""

        return bool(re.search(r"(?<!\w)" + re.escape(phrase) + r"(?!\w)", text))


class _QwenClassification(BaseModel):
    """Strict, task-local response contract for Communication."""

    model_config = ConfigDict(extra="forbid")
    category: Category
    confidence: float = Field(ge=0, le=1)
    note: str | None = Field(default=None, max_length=240)


class QwenClassifier(BaseClassifier):
    """Campus Qwen adapter for Communication intent classification.

    This adapter classifies only into the application's existing categories.
    It cannot determine identity, property scope, approval, emergency status,
    or delivery. Any unavailable, uncertain, malformed, or unsupported model
    output returns ``None`` so the service's existing human-handoff path wins.
    """

    def __init__(self, *, base_url: str | None = None, api_key: str | None = None,
                 model: str | None = None):
        configuration = get_configuration()
        self._base_url = (base_url or configuration.base_url).strip()
        self._api_key = (api_key or configuration.api_key).strip()
        self._model = (model or configuration.model).strip()

    @property
    def configured(self) -> bool:
        return bool(self._base_url and self._api_key and self._model)

    def classify(self, content: str) -> Classification | None:
        if not self.configured:
            return None
        try:
            with httpx.Client(
                timeout=float(os.getenv("LLM_TIMEOUT_SECONDS", "30")),
                trust_env=False,
            ) as client:
                response = client.post(
                    self._base_url.rstrip("/") + "/chat/completions",
                    headers={"Authorization": "Bearer " + self._api_key},
                    json={
                        "model": self._model,
                        "messages": [
                            {
                                "role": "system",
                                "content": (
                                    "Classify one tenant property-management message. "
                                    "Return only JSON with category, confidence and optional note. "
                                    "Allowed category values are maintenance, lease_rent, "
                                    "smalltalk, and unknown. Map complaints or general enquiries "
                                    "to maintenance/lease_rent only when their intent is clear; "
                                    "otherwise use unknown. Do not decide identity, property "
                                    "ownership, emergency status, approval, or delivery."
                                ),
                            },
                            {"role": "user", "content": content},
                        ],
                        "temperature": 0,
                        "max_completion_tokens": 160,
                        "reasoning_effort": "none",
                        "stream": False,
                    },
                )
            response.raise_for_status()
            raw = response.json()["choices"][0]["message"]["content"]
            if not isinstance(raw, str) or not raw.strip():
                return None
            parsed = _QwenClassification.model_validate(json.loads(raw.strip()))
            if parsed.confidence < 0.6:
                return None
            return Classification(parsed.category, parsed.confidence, parsed.note or "qwen")
        except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError):
            # Never expose raw model responses, credentials, or tenant content.
            return None


def build_communication_classifier() -> BaseClassifier:
    """Use Campus Qwen only when fully configured; otherwise use safe mode."""

    candidate = QwenClassifier()
    return candidate if candidate.configured else KeywordClassifier()
