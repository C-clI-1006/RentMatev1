import json
from datetime import date, datetime, timezone

import httpx
import pytest

from contracts.common import LeaseId, PropertyId, RentRecordId, RequestId
from contracts.maintenance import MaintenanceStatus, Priority
from contracts.reporting import (
    LeaseRef,
    LeaseSectionData,
    MaintenanceRequestRef,
    MaintenanceSectionData,
    NarrativeSource,
    RentRecordRef,
    RentSectionData,
    ReportNarrative,
    ReportPeriod,
    SectionAvailability,
    SourceReference,
)
from llm.reporting import LlmReportNarrator
from reporting.collector import CollectedReportData

_REF = SourceReference(
    source_type="maintenance_request", source_id="req-test", version="1"
)
_TS = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)


def _data() -> CollectedReportData:
    return CollectedReportData(
        property_id=PropertyId("prop-test"),
        period=ReportPeriod(year=2026, month=9),
        maintenance=MaintenanceSectionData(
            availability=SectionAvailability.AVAILABLE,
            requests=[
                MaintenanceRequestRef(
                    request_id=RequestId("req-test"),
                    title="Loose door handle",
                    status=MaintenanceStatus.IN_PROGRESS,
                    priority=Priority.ROUTINE,
                    created_at=_TS,
                    last_activity_at=_TS,
                    references=[_REF],
                )
            ],
        ),
        rent=RentSectionData(
            availability=SectionAvailability.AVAILABLE,
            records=[
                RentRecordRef(
                    rent_record_id=RentRecordId("rr-test"),
                    period="2026-09",
                    status="paid",
                    references=[
                        SourceReference(
                            source_type="rent_record", source_id="rr-test"
                        )
                    ],
                )
            ],
        ),
        lease=LeaseSectionData(
            availability=SectionAvailability.NOT_AVAILABLE,
            note="lease source unavailable for test",
        ),
    )


def _call(monkeypatch):
    return LlmReportNarrator()(_data())


@pytest.fixture
def config(monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "http://proxy.invalid/v1")
    monkeypatch.setenv("LLM_API_KEY", "test-only-key")
    monkeypatch.setenv("LLM_MODEL", "campus-qwen")


def _respond(monkeypatch, content):
    def post(self, url, **kwargs):
        assert url == "http://proxy.invalid/v1/chat/completions"
        assert kwargs["headers"]["Authorization"] == "Bearer test-only-key"
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={"choices": [{"message": {"content": content}}]},
        )

    monkeypatch.setattr(httpx.Client, "post", post)


def _capture_request(monkeypatch):
    """Patch the client so the sent body is recorded and a valid reply returned."""
    captured = {}

    def post(self, url, **kwargs):
        captured["body"] = kwargs["json"]
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={"choices": [{"message": {"content": '{"summary": "ok"}'}}]},
        )

    monkeypatch.setattr(httpx.Client, "post", post)
    return captured


def _system_prompt(body):
    return body["messages"][0]["content"]


def _user_payload(body):
    return json.loads(body["messages"][1]["content"])


def test_system_prompt_distinguishes_empty_from_unavailable(config, monkeypatch):
    captured = _capture_request(monkeypatch)
    _call(monkeypatch)
    prompt = _system_prompt(captured["body"]).lower()
    # The prompt must teach all three states and keep them apart.
    assert "available" in prompt
    assert "empty" in prompt
    assert "not_available" in prompt
    # EMPTY means zero records, and must not be called "unavailable".
    assert "no records this period" in prompt
    assert "data unavailable" in prompt
    assert "unavailable" in prompt  # the ban on conflating the states
    # The prompt must forbid the two confusions explicitly.
    assert "never" in prompt


def test_user_payload_is_self_explanatory(config, monkeypatch):
    captured = _capture_request(monkeypatch)
    _call(monkeypatch)
    payload = _user_payload(captured["body"])
    # Per-section availability must be present in the payload itself.
    assert payload["maintenance"]["availability"] == "available"
    assert payload["rent"]["availability"] == "available"
    assert payload["lease"]["availability"] == "not_available"
    # The NOT_AVAILABLE note must travel with the payload.
    assert payload["lease"]["note"] == "lease source unavailable for test"
    # A legend key must define each state, separating empty from unavailable.
    legend = payload["availability_legend"]
    assert "available" in legend
    assert "empty" in legend
    assert "not_available" in legend
    # EMPTY must be described as "no records", NOT "unavailable".
    assert "unavailable" not in legend["empty"].lower()
    # NOT_AVAILABLE must be described as "unavailable".
    assert "unavailable" in legend["not_available"].lower()


def test_unconfigured_returns_missing_without_network(monkeypatch):
    for var in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL"):
        monkeypatch.delenv(var, raising=False)
    result = _call(monkeypatch)
    assert result.source is NarrativeSource.MISSING
    assert result.text is None


def test_valid_json_returns_live_summary(config, monkeypatch):
    _respond(monkeypatch, json.dumps({"summary": "One routine request."}))
    result = _call(monkeypatch)
    assert result.source is NarrativeSource.LIVE
    assert result.text == "One routine request."
    assert result.generated_at is not None


def test_request_disables_thinking(config, monkeypatch):
    def post(self, url, **kwargs):
        body = kwargs["json"]
        assert body["reasoning_effort"] == "none"
        assert body["max_completion_tokens"] == 800
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={
                "choices": [{"message": {"content": '{"summary": "ok"}'}}]
            },
        )

    monkeypatch.setattr(httpx.Client, "post", post)
    assert _call(monkeypatch).source is NarrativeSource.LIVE


@pytest.mark.parametrize("content", [None, "", "   "])
def test_blank_content_returns_missing_without_crash(config, monkeypatch, content):
    _respond(monkeypatch, content)
    result = _call(monkeypatch)
    assert result.source is NarrativeSource.MISSING
    assert result.text is None


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        '{"summary": "", "note": "extra key"}',  # extra key + empty summary
        '{"needs_review": true}',
    ],
)
def test_invalid_or_unexpected_output_fails_closed(config, monkeypatch, content):
    _respond(monkeypatch, content)
    result = _call(monkeypatch)
    assert result.source is NarrativeSource.MISSING
    assert result.text is None


def test_http_error_returns_missing(config, monkeypatch):
    monkeypatch.setattr(
        httpx.Client,
        "post",
        lambda self, url, **kw: httpx.Response(
            500, request=httpx.Request("POST", url), json={"error": "boom"}
        ),
    )
    result = _call(monkeypatch)
    assert result.source is NarrativeSource.MISSING
    assert result.text is None


def test_timeout_returns_missing_without_leaking_secret(config, monkeypatch):
    def fail(*args, **kwargs):
        raise httpx.ConnectTimeout("test-only-key")

    monkeypatch.setattr(httpx.Client, "post", fail)
    result = _call(monkeypatch)
    assert result.source is NarrativeSource.MISSING
    assert result.text is None
    assert result.failure_code == "timeout"
    assert "test-only-key" not in result.model_dump_json()


def test_fenced_json_is_validated_instead_of_discarding_real_summary(config, monkeypatch):
    _respond(monkeypatch, '```json\n{"summary":"One routine repair."}\n```')
    result = _call(monkeypatch)
    assert result.source is NarrativeSource.LIVE
    assert result.text == "One routine repair."
    assert result.failure_code is None


@pytest.mark.parametrize("first_choice", [
    {"message": {"content": ""}, "finish_reason": "stop"},
    {"message": {"content": '{"summary":"partial'}, "finish_reason": "length"},
])
def test_empty_or_truncated_response_retries_once_with_larger_budget(config, monkeypatch, first_choice):
    budgets = []

    def post(self, url, **kwargs):
        budgets.append(kwargs["json"]["max_completion_tokens"])
        choice = first_choice if len(budgets) == 1 else {"message": {"content": '{"summary":"Recovered real summary."}'}}
        return httpx.Response(200, request=httpx.Request("POST", url), json={"choices": [choice]})

    monkeypatch.setattr(httpx.Client, "post", post)
    result = _call(monkeypatch)
    assert result.source is NarrativeSource.LIVE
    assert result.text == "Recovered real summary."
    assert budgets == [800, 1600]


def test_auth_rejection_is_not_retried_and_has_only_safe_diagnostic(config, monkeypatch):
    calls = []

    def post(self, url, **kwargs):
        calls.append(url)
        return httpx.Response(401, request=httpx.Request("POST", url), text="test-only-key")

    monkeypatch.setattr(httpx.Client, "post", post)
    result = _call(monkeypatch)
    assert len(calls) == 1
    assert result.failure_code == "http_rejected"
    assert result.text is None
    assert "test-only-key" not in result.model_dump_json()


def test_whitespace_summary_is_not_claimed_as_live(config, monkeypatch):
    _respond(monkeypatch, '{"summary":"   "}')
    result = _call(monkeypatch)
    assert result.source is NarrativeSource.MISSING
    assert result.failure_code == "invalid_output"


def test_unconfigured_failure_is_explicit(monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "")
    result = _call(monkeypatch)
    assert result.source is NarrativeSource.MISSING
    assert result.failure_code == "not_configured"
