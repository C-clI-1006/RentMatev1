import json

import httpx
import pytest

from contracts.maintenance import Priority
from llm import Assessment, AssessmentFailure
from llm.live import LlmPriorityAssessor


def invoke():
    return LlmPriorityAssessor().assess(
        title="Handle",
        description="Loose door handle",
        location="Bedroom",
        intake_emergency_flag=False,
    )


@pytest.fixture
def config(monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "http://proxy.invalid/v1")
    monkeypatch.setenv("LLM_API_KEY", "test-only-key")
    monkeypatch.setenv("LLM_MODEL", "campus-qwen")


def test_live_adapter_validated_response(config, monkeypatch):
    def post(self, url, **kwargs):
        assert url == "http://proxy.invalid/v1/chat/completions"
        assert kwargs["json"]["model"] == "campus-qwen"
        assert kwargs["headers"]["Authorization"] == "Bearer test-only-key"
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "priority": "routine",
                                    "rationale": "Loose handle with no immediate hazard",
                                    "confidence": 0.8,
                                    "emergency_indicator": False,
                                    "provider_suggestion": "handyman",
                                }
                            )
                        }
                    }
                ]
            },
        )

    monkeypatch.setattr(httpx.Client, "post", post)
    r = invoke()
    assert isinstance(r, Assessment) and r.source == "llm" and r.priority == Priority.ROUTINE


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        '{"needs_review":true}',
        '{"priority":"routine","rationale":"x","confidence":4,"emergency_indicator":false}',
        '{"priority":"routine","rationale":"x","confidence":0.1,"emergency_indicator":false}',
    ],
)
def test_invalid_or_uncertain_output_fails_closed(config, monkeypatch, content):
    monkeypatch.setattr(
        httpx.Client,
        "post",
        lambda self, url, **kw: httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={"choices": [{"message": {"content": content}}]},
        ),
    )
    assert isinstance(invoke(), AssessmentFailure)


def test_timeout_does_not_leak_secret(config, monkeypatch):
    def fail(*args, **kw):
        raise httpx.ConnectTimeout("test-only-key")

    monkeypatch.setattr(httpx.Client, "post", fail)
    result = invoke()
    assert isinstance(result, AssessmentFailure) and "test-only-key" not in result.detail


def test_unconfigured_is_manual_review_not_stub(monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    assert isinstance(invoke(), AssessmentFailure)


def test_model_requested_review_is_not_network_failure(config, monkeypatch):
    monkeypatch.setattr(
        httpx.Client,
        "post",
        lambda self, url, **kw: httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={"choices": [{"message": {"content": '{"needs_review":true}'}}]},
        ),
    )
    assert invoke().code == "missing_info"
