"""Deterministic send-eligibility policy for tenant replies (FR-COM-04/05).

Eligibility requires a recognised routine message and the appropriate business
result. Unknown phrasing falls back to review. Explicit emergency and decision
signals can only escalate review; model confidence never grants permission.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re

from contracts.drafting import DraftStatus

__all__ = [
    "ReviewReason",
    "EligibilityDecision",
    "EmergencyAlert",
    "is_decision_request",
    "decide",
]


class ReviewReason(str, Enum):
    """Closed list of reasons a draft is routed to manager review."""

    EMERGENCY = "emergency"
    CONFLICT = "conflict"
    UNCERTAIN = "uncertain"
    DECISION_REQUEST = "decision_request"
    UNAVAILABLE_FACT = "unavailable_fact"


# Triggers rank by severity when several apply (used to pick one stored reason).
_SEVERITY = [
    ReviewReason.EMERGENCY,
    ReviewReason.CONFLICT,
    ReviewReason.UNCERTAIN,
    ReviewReason.DECISION_REQUEST,
    ReviewReason.UNAVAILABLE_FACT,
]

# Closed list of phrasings that request a financial / contractual / legal
# DECISION from the manager. Factual questions ("how much rent is due?",
# "when does my lease end?") match none of these and stay auto-eligible.
# Direction is one-way: matching only escalates to review.
_DECISION_PHRASES = (
    "reduce",
    "waive",
    "discount",
    "refund",
    "negotiate",
    "change my lease",
    "modify",
    "extend my lease",
    "renew my lease",
    "break the lease",
    "early termination",
    "cancel my lease",
    "sublet",
    "assign my lease",
    "move out early",
)


def is_decision_request(text: str) -> bool:
    """True when the message asks for a financial/contractual/legal decision.

    Deterministic closed list; never used to grant auto-eligibility.
    """
    lowered = text.lower()
    return any(phrase in lowered for phrase in _DECISION_PHRASES)


@dataclass(frozen=True)
class EligibilityDecision:
    """Eligibility outcome for one draft.

    ``reason`` is set only when the draft is routed to review.
    """

    status: DraftStatus
    reason: ReviewReason | None


@dataclass(frozen=True)
class EmergencyAlert:
    """Honest record of the manager-alert attempt for an emergency.

    ``dispatched`` means the notification publisher ACCEPTED the event; it
    is never claimed as delivered. ``failure`` records why the alert could
    not be dispatched (no manager bound, publisher rejected/raised).
    """

    dispatched: bool
    manager_ids: tuple[str, ...] = ()
    failure: str | None = None


def decide(auto_eligible: bool, *triggers: ReviewReason) -> EligibilityDecision:
    """Apply the policy.

    Any review trigger wins over auto-eligibility. With no trigger, the
    draft is auto-eligible only when the business result is verified and
    factual; otherwise it conservatively goes to review.
    """
    if triggers:
        for reason in _SEVERITY:
            if reason in triggers:
                return EligibilityDecision(DraftStatus.PENDING_REVIEW, reason)
        raise ValueError(f"unknown review trigger in {triggers!r}")
    if auto_eligible:
        return EligibilityDecision(DraftStatus.AUTO_ELIGIBLE, None)
    return EligibilityDecision(DraftStatus.PENDING_REVIEW, ReviewReason.UNCERTAIN)


# Explicit prototype coverage, not a semantic classifier. Unknown phrasing
# falls back to review. These rules never use model confidence as authority.
DEFAULT_EMERGENCY_TERMS = (
    "fire", "smoke", "gas leak", "gas smell", "smell of gas", "smell gas",
    "flood", "flooding",
    "burst pipe", "sparking", "carbon monoxide", "no water", "no heat",
)


def has_emergency_indicator(text, terms=DEFAULT_EMERGENCY_TERMS):
    """Detect unnegated emergency phrases and conservatively escalate them.

    Explicit local negations are removed per phrase. This prevents ``no gas
    smell`` from raising an alert while still detecting a later positive claim
    in the same message. Uncertain wording is not treated as a negation, so it
    remains review-required rather than becoming automatically eligible.
    """

    searchable = " ".join(str(text).casefold().replace("’", "'").split())
    for term in terms:
        phrase = re.escape(term.casefold()).replace(r"\ ", r"\s+")
        negated = re.compile(
            r"(?:\bno\b|\bnot\b|\bwithout\b|\bcannot\b|\bcan't\b|"
            r"\bdo\s+not\b|\bdon't\b|\bdid\s+not\b|\bdidn't\b)"
            r"(?:\s+\w+){0,3}\s+" + phrase + r"\b",
            re.I,
        )
        searchable = negated.sub(" ", searchable)
    return any(
        re.search(r"\b" + re.escape(term) + r"\b", searchable, re.I)
        for term in terms
    )


def _normalise(text):
    return " ".join(text.casefold().strip().rstrip(".!?").split())


def is_routine_greeting(text):
    return _normalise(text) in {"hi", "hello", "thanks", "thank you", "good morning", "hi, good morning"}


def is_routine_fact_question(text):
    # A full match prevents a factual prefix from authorising extra requests.
    return bool(re.fullmatch(
        r"(?:how much (?:is my rent|rent is due)|"
        r"what is my (?:rent amount|lease (?:start|end) date)|"
        r"when does my lease (?:start|end)|"
        r"(?:have you received my rent payment(?: yet)?|has my rent been paid))",
        _normalise(text)))


def is_routine_maintenance(text):
    return bool(re.fullmatch(
        r"(?:(?:my|the) )?(?:(?:kitchen|bathroom|bedroom) )?"
        r"(?:heater|sink|stove|oven|fridge|toilet|plumbing|door|window|something) "
        r"is (?:broken|leaking(?: badly)?|not working)", _normalise(text)))
