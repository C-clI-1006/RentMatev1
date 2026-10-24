"""Opt-in real proxy/API acceptance check using synthetic data and a temporary DB.

Run from backend: python -m maintenance.verify_live --output path/to/result.json
Requires LLM_BASE_URL, LLM_MODEL, LLM_API_KEY and reachable course proxy.
Does not send tenant notifications, connect other agents, or expose credentials.
"""

import argparse
import json
import os
import secrets
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from main import app
from maintenance.api import get_store
from maintenance.store import Store


def run():
    token, manager_token = secrets.token_hex(24), secrets.token_hex(24)
    previous = os.environ.get("RENTMATE_AUTH_TOKENS")
    os.environ["RENTMATE_AUTH_TOKENS"] = json.dumps(
        {
            token: {"tenant_id": "acceptance-tenant", "property_id": "acceptance-property"},
            manager_token: {
                "tenant_id": "acceptance-tenant",
                "property_id": "acceptance-property",
                "manager_id": "acceptance-manager",
            },
        }
    )
    report = {
        "checked_at_utc": datetime.now(UTC).isoformat(),
        "model": os.getenv("LLM_MODEL"),
        "cases": [],
        "checks": {},
        "boundary": "Synthetic API requests; real model; no real tenant/evidence consumer delivery.",
    }
    try:
        with tempfile.TemporaryDirectory(prefix="rentmate-live-") as directory:
            store = Store(Path(directory) / "acceptance.db")
            app.dependency_overrides[get_store] = lambda: store
            tenant_headers = {"Authorization": "Bearer " + token}
            manager_headers = {"Authorization": "Bearer " + manager_token}
            with TestClient(app) as client:
                cases = [
                    (
                        "routine",
                        "Loose cupboard handle",
                        "The kitchen cupboard handle is loose but the cupboard works. No injury or immediate hazard.",
                    ),
                    (
                        "urgent",
                        "No hot water",
                        "There has been no hot water from any tap since yesterday. Cold water works. No gas smell, smoke or injury.",
                    ),
                    (
                        "emergency",
                        "Gas leak",
                        "There is a strong gas smell and hissing from the kitchen gas pipe.",
                    ),
                    (
                        "uncertain",
                        "Something wrong",
                        "Something is wrong. I cannot explain what or where.",
                    ),
                ]
                records = {}
                for name, title, description in cases:
                    start = time.monotonic()
                    body = {
                        "message_id": "acceptance-" + name,
                        "event_id": "event-" + name,
                        "title": title,
                        "description": description,
                    }
                    response = client.post(
                        "/api/maintenance/requests", json=body, headers=tenant_headers
                    )
                    response.raise_for_status()
                    intake = response.json()
                    record = client.get(
                        "/api/maintenance/requests/" + intake["request_id"], headers=manager_headers
                    ).json()
                    assessment_reason = record["events"][-1]["reason"]
                    try:
                        assessment = json.loads(assessment_reason)
                    except (ValueError, TypeError):
                        assessment = {"review_reason": assessment_reason}
                    passed = (
                        record["status"] == "under_review"
                        if name == "uncertain"
                        else (
                            record["status"] == "assessed"
                            and record["priority"] == name
                            and assessment.get("source") == "llm"
                        )
                    )
                    report["cases"].append(
                        {
                            "case": name,
                            "passed": passed,
                            "elapsed_seconds": round(time.monotonic() - start, 2),
                            "status": record["status"],
                            "priority": record["priority"],
                            "assessment": assessment,
                        }
                    )
                    records[name] = record
                    if name == "routine":
                        replay = client.post(
                            "/api/maintenance/requests", json=body, headers=tenant_headers
                        ).json()
                        report["checks"]["duplicate_same_request"] = (
                            replay["request_id"] == record["request_id"]
                        )
                record = records["routine"]
                request_id = record["request_id"]
                for state in ["approved", "in_progress", "awaiting_confirmation", "closed"]:
                    response = client.post(
                        f"/api/maintenance/requests/{request_id}/status",
                        headers=manager_headers,
                        json={
                            "expected_version": record["version"],
                            "target_status": state,
                            "reason": "Synthetic acceptance manager decision: " + state,
                            "evidence_refs": ["synthetic-evidence-reference"],
                        },
                    )
                    response.raise_for_status()
                    record = client.get(
                        "/api/maintenance/requests/" + request_id, headers=manager_headers
                    ).json()
                report["checks"]["approved_lifecycle_closed"] = record["status"] == "closed"
                report["checks"]["history_persisted"] = len(record["events"]) == 6
                report["checks"]["reporting_summary"] = any(
                    r["request_id"] == request_id and r["status"] == "closed"
                    for r in client.get(
                        "/api/maintenance/summaries", headers=manager_headers
                    ).json()
                )
                queue = client.get("/api/maintenance/outbox", headers=manager_headers).json()
                report["checks"]["emergency_alert_queued"] = any(
                    r["target"] == "manager_alert"
                    and r["request_id"] == records["emergency"]["request_id"]
                    for r in queue
                )
                report["checks"]["delivery_truthfully_pending"] = all(
                    r["status"] == "pending" for r in queue
                )
        report["passed"] = all(c["passed"] for c in report["cases"]) and all(
            report["checks"].values()
        )
        return report
    finally:
        app.dependency_overrides.pop(get_store, None)
        if previous is None:
            os.environ.pop("RENTMATE_AUTH_TOKENS", None)
        else:
            os.environ["RENTMATE_AUTH_TOKENS"] = previous


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    result = run()
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["passed"] else 1)
