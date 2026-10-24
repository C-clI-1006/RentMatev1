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
