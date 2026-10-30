"""Standalone live check for the monthly-report narrator.

Purpose: confirm, against the REAL model, that EMPTY (source reachable,
zero records) and NOT_AVAILABLE (source failed) are described
distinctly in the prose — EMPTY must not read as "data unavailable",
and NOT_AVAILABLE must not read as "no records".

Deliberately NOT a pytest test: tests/conftest.py blocks external
sockets and blanks the LLM_* env for the whole test session. This
script therefore runs standalone, reads backend/.env in-process,
persists nothing, and never prints credentials.

Run from backend/:
    .venv/bin/python scripts/verify_narrator_live.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

BACKEND = Path(__file__).resolve().parents[1]
load_dotenv(BACKEND / ".env")  # never overrides already-set vars

sys.path.insert(0, str(BACKEND))

from contracts.common import PropertyId, RentRecordId  # noqa: E402
from contracts.reporting import (  # noqa: E402
    LeaseSectionData,
    RentRecordRef,
    RentSectionData,
    ReportPeriod,
    SectionAvailability,
    SourceReference,
    MaintenanceSectionData,
)
from llm.configuration import ModelConfiguration, set_configuration  # noqa: E402
from llm.reporting import LlmReportNarrator  # noqa: E402
from reporting.collector import CollectedReportData  # noqa: E402

base_url = os.environ.get("LLM_BASE_URL", "").strip()
model = os.environ.get("LLM_MODEL", "").strip()
api_key = os.environ.get("LLM_API_KEY", "").strip()

if not all((base_url, model, api_key)):
    raise SystemExit(
        "LLM_BASE_URL / LLM_MODEL / LLM_API_KEY not all set in "
        "backend/.env; cannot run the live check."
    )

# Runtime override (preferred by get_configuration) keeps this process
# explicit about which endpoint/model it is calling.
set_configuration(
    ModelConfiguration(base_url=base_url, model=model, api_key=api_key)
)
print(f"Calling model={model} at base_url={base_url}\n")

PERIOD = ReportPeriod(year=2026, month=9)


def _empty_all() -> CollectedReportData:
    """Every source reachable, every section holding zero in-scope records."""
    return CollectedReportData(
        property_id=PropertyId("prop-live-check"),
        period=PERIOD,
        maintenance=MaintenanceSectionData(
            availability=SectionAvailability.EMPTY
        ),
        rent=RentSectionData(availability=SectionAvailability.EMPTY),
        lease=LeaseSectionData(availability=SectionAvailability.EMPTY),
    )


def _one_not_available() -> CollectedReportData:
    """Maintenance/rent available; lease source failed with a note."""
    return CollectedReportData(
        property_id=PropertyId("prop-live-check"),
        period=PERIOD,
        maintenance=MaintenanceSectionData(
            availability=SectionAvailability.EMPTY
        ),
        rent=RentSectionData(
            availability=SectionAvailability.AVAILABLE,
            records=[
                RentRecordRef(
                    rent_record_id=RentRecordId("rr-live"),
                    period="2026-09",
                    status="paid",
                    references=[
                        SourceReference(
                            source_type="rent_record", source_id="rr-live"
                        )
                    ],
                )
            ],
        ),
        lease=LeaseSectionData(
            availability=SectionAvailability.NOT_AVAILABLE,
            note="lease source temporarily unreachable during collection",
        ),
    )


def _run(title: str, data: CollectedReportData) -> None:
    print(f"=== {title} ===")
    result = LlmReportNarrator()(data)
    print(f"source={result.source.value}")
    if result.failure_code:
        print(f"failure_code={result.failure_code}")
    if result.text:
        print("--- summary ---")
        print(result.text)
    else:
        print("no text produced")
    print()


_run("Scenario A — all sections EMPTY (reachable, zero records)",
     _empty_all())
_run("Scenario B — lease NOT_AVAILABLE (with note), rent AVAILABLE, "
     "maintenance EMPTY", _one_not_available())
