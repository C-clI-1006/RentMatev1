"""Preset reply templates for tenant communication.

These are the reliable baseline for draft replies. Every builder takes only
verified, system-provided inputs (request IDs, persisted statuses, authorised
facts, named missing fields). They never invent contractor names, promised
times, completion dates, registration claims, policy claims, or success
claims. An optional LLM wording pass may later rephrase output, but it may
only use these supplied facts and may not change commitments or eligibility.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date, datetime

from contracts.lease_rent import Fact

__all__ = [
    "maintenance_confirmation",
    "clarification",
    "rent_lease_facts",
    "unavailable_fact",
    "service_failure",
    "human_handoff",
    "smalltalk",
]


def maintenance_confirmation(request_id: str, status: str) -> str:
    """Acknowledge a registered maintenance request with its real ID and the
    status actually persisted by the maintenance service.

    Never promises a contractor, visit time, or completion date.
    """
    return (f"Your maintenance request is registered (ref {request_id}). "
            f"Recorded status: {status}. No appointment or completion time is confirmed.")


def clarification(missing_fields: Iterable[str]) -> str:
    """Ask for the specific fields the intake service said were missing.

    Never claims the issue is already registered — no request exists yet.
    """
    # Do not echo arbitrary provider diagnostics/field names to a tenant.
    labels = {"location": "location", "description": "issue description",
              "title": "brief description", "date started": "when it started",
              "started_at": "when it started"}
    fields = list(dict.fromkeys(labels.get(f, "additional issue details") for f in missing_fields))
    return ("Please provide: " + ", ".join(fields or ["additional issue details"]) +
            ". No maintenance request was created by this intake.")


def rent_lease_facts(facts: Sequence[Fact]) -> str:
    """Answer a factual rent/lease question using only the returned
    authorised facts. Each value keeps its as-of date; source references are
    carried in draft metadata (source_facts), not restated here."""
    lines = []
    for fact in facts:
        as_of = _format_as_of(fact.as_of)
        lines.append(f"{fact.name}: {fact.value}" + (f" (as of {as_of})" if as_of else ""))
    body = "Here's what our records show: " + "; ".join(lines) + "."
    return (
        body
        + " For anything not listed here, ask us and we'll check rather "
        "than guess."
    )


def unavailable_fact(question_name: str) -> str:
    """State that a requested fact could not be verified. Never infers that a
    rule, clause, or payment does not exist."""
    return "I couldn't find verified information to answer this question."


def service_failure(service_name: str) -> str:
    """State honestly that a dependency was unavailable. No invented success
    acknowledgement."""
    return "The request outcome could not be confirmed. Please contact the property office."


def human_handoff(notified: bool) -> str:
    """Handoff to a human. Says 'forwarded / notified' only when that action
    was actually recorded (``notified=True``)."""
    if notified:
        return "Your message has been forwarded for manager review."
    return "This needs manager review. Please contact the property office."


def smalltalk() -> str:
    """Short, neutral smalltalk acknowledgement with no business promises."""
    return "Thanks for the note. Is there anything about your tenancy I can help with?"


def _format_as_of(as_of: date | datetime | None) -> str:
    if as_of is None:
        return ""
    if isinstance(as_of, datetime):
        return as_of.date().isoformat()
    return as_of.isoformat()
