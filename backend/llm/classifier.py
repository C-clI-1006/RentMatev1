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
import re


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
