"""Conservative English prototype indicators; operational, not statutory categories."""

import re

from llm.assess import DEFAULT_EMERGENCY_KEYWORDS

# Only suppress explicit denials immediately before a hazard or in a short
# coordinated hazard list ("no gas smell, smoke or fire"). Contrast clauses
# reset the denial. This is not a general natural-language safety classifier.
DENIAL = re.compile(
    r"\b(?:no|without|denies)\s+(?:(?:any|signs?|of|gas|smell|smoke|fire|flooding|flood|sparking|carbon|monoxide|and|or)\b[\s,]*)*$",
    re.IGNORECASE,
)


def emergency_indicators(text):
    hits = []
    for keyword in DEFAULT_EMERGENCY_KEYWORDS:
        for match in re.finditer(rf"\b{re.escape(keyword)}\b", text, re.IGNORECASE):
            prefix = re.split(
                r"[.;!?]|\b(?:but|however|yet)\b", text[: match.start()], flags=re.IGNORECASE
            )[-1]
            if not DENIAL.search(prefix):
                hits.append(keyword)
                break
    return hits
