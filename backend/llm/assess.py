"""Deterministic keyword assessor — a LABELLED test double (guide 19).

This is NOT inference. It exists so the maintenance module (and its
tests) run without the school Qwen proxy, while keeping the exact
`IPriorityAssessor` interface shape. Its output is labelled
`source="stub"`. Do not present results from this double as evidence
of a live agent.

Rules (deterministic, in priority order):
1. The intake `emergency_flag` (set by the communication module from
   its configured emergency categories) is a floor: it always yields
   Emergency with `emergency_indicator=True`.
2. Otherwise, configured keyword sets decide:
   - any emergency keyword -> Emergency (emergency_indicator=True)
   - else any urgent keyword -> Urgent
   - else any routine keyword -> Routine
   - else -> AssessmentFailure(code="missing_info") with low signal;
     the request is routed to UnderReview (UC3 A2), never guessed.
3. If keywords from two different priority tiers match, the assessment
   is a conflict -> AssessmentFailure(code="conflict") (UC3 A2: route
   to UnderReview, do not infer an outcome).

The keyword sets are constructor parameters so priority categories and
emergency rules stay configurable (Stage 1 report, §3.2).
"""

from __future__ import annotations

import re

from contracts.maintenance import Priority

from . import Assessment, AssessmentFailure, IPriorityAssessor

DEFAULT_EMERGENCY_KEYWORDS: tuple[str, ...] = (
    "fire",
    "smoke",
    "gas leak",
    "gas smell",
    "flooding",
    "flood",
    "burst pipe",
    "no water",
    "no heat",
    "no heating",
    "electrical fire",
    "sparking",
    "carbon monoxide",
)

DEFAULT_URGENT_KEYWORDS: tuple[str, ...] = (
    "no hot water",
    "leak",
    "locked out",
    "no power",
    "broken",
    "not working",
    "plumbing",
    "toilet blocked",
    "heater broken",
)

DEFAULT_ROUTINE_KEYWORDS: tuple[str, ...] = (
    "paint",
    "curtain",
    "scratch",
    "loose handle",
    "door handle",
    "minor",
    "graffiti",
    "window screen",
)

DEFAULT_PROVIDER_HINTS: dict[str, tuple[str, ...]] = {
    "plumber": ("water", "pipe", "plumbing", "toilet", "leak", "hot water"),
    "electrician": ("power", "electric", "socket", "spark", "light"),
    "locksmith": ("lock", "key"),
    "handyman": ("handle", "paint", "screen", "curtain"),
}


def _hits(text: str, keywords: tuple[str, ...]) -> list[str]:
    lowered = text.lower()
    return [kw for kw in keywords if re.search(rf"\b{re.escape(kw)}\b", lowered)]


class KeywordPriorityAssessor(IPriorityAssessor):
    """Labelled stub assessor (deterministic keyword matching)."""

    def __init__(
        self,
        emergency_keywords: tuple[str, ...] = DEFAULT_EMERGENCY_KEYWORDS,
        urgent_keywords: tuple[str, ...] = DEFAULT_URGENT_KEYWORDS,
        routine_keywords: tuple[str, ...] = DEFAULT_ROUTINE_KEYWORDS,
        provider_hints: dict[str, tuple[str, ...]] | None = None,
    ) -> None:
        self._emergency = tuple(emergency_keywords)
        self._urgent = tuple(urgent_keywords)
        self._routine = tuple(routine_keywords)
        self._providers = provider_hints if provider_hints is not None else DEFAULT_PROVIDER_HINTS

    def assess(
        self,
        *,
        title: str,
        description: str,
        location: str | None,
        intake_emergency_flag: bool,
    ) -> Assessment | AssessmentFailure:
        text = f"{title}\n{description}\n{location or ''}"

        # 1. Explicit intake flag is a floor — never downgraded.
        if intake_emergency_flag:
            return Assessment(
                priority=Priority.EMERGENCY,
                rationale="Intake carried the emergency flag from the "
                "communication module's configured emergency categories.",
                confidence=0.95,
                emergency_indicator=True,
                provider_suggestion=self._suggest_provider(text),
                source="stub",
            )

        emergency_hits = _hits(text, self._emergency)
        urgent_hits = _hits(text, self._urgent)
        routine_hits = _hits(text, self._routine)

        tiers = {
            tier
            for tier, hits in (
                ("emergency", emergency_hits),
                ("urgent", urgent_hits),
                ("routine", routine_hits),
            )
            if hits
        }

        # 3. Conflicting signals -> UnderReview, do not guess (UC3 A2).
        if len(tiers) > 1:
            return AssessmentFailure(
                code="conflict",
                detail=(
                    "Conflicting priority signals: emergency="
                    f"{emergency_hits}, urgent={urgent_hits}, routine={routine_hits}."
                ),
            )

        if emergency_hits:
            return Assessment(
                priority=Priority.EMERGENCY,
                rationale=f"Emergency keywords matched: {emergency_hits}.",
                confidence=0.8,
                emergency_indicator=True,
                provider_suggestion=self._suggest_provider(text),
                source="stub",
            )
        if urgent_hits:
            return Assessment(
                priority=Priority.URGENT,
                rationale=f"Urgent keywords matched: {urgent_hits}.",
                confidence=0.7,
                emergency_indicator=False,
                provider_suggestion=self._suggest_provider(text),
                source="stub",
            )
        if routine_hits:
            return Assessment(
                priority=Priority.ROUTINE,
                rationale=f"Routine keywords matched: {routine_hits}.",
                confidence=0.6,
                emergency_indicator=False,
                provider_suggestion=self._suggest_provider(text),
                source="stub",
            )

        # 2. No usable signal: missing information, not a failure of the
        # request — route to UnderReview (UC3 A2).
        return AssessmentFailure(
            code="missing_info",
            detail="No configured priority signal found in the issue text.",
        )

    def _suggest_provider(self, text: str) -> str | None:
        """Optional FR-MC-08: a provider-type hint with no authority."""
        lowered = text.lower()
        for provider, hints in self._providers.items():
            if any(h in lowered for h in hints):
                return provider
        return None
