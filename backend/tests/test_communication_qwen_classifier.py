import json

import httpx
import pytest

from llm.classifier import Category, KeywordClassifier, QwenClassifier, build_communication_classifier


def _configure(monkeypatch):
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


def test_qwen_classifier_validates_supported_category(monkeypatch):
    _configure(monkeypatch)
    _respond(monkeypatch, json.dumps({"category": "lease_rent", "confidence": 0.94}))
    result = QwenClassifier().classify("Has my rent been paid?")
    assert result.category is Category.LEASE_RENT
    assert result.note == "qwen"


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        json.dumps({"category": "complaint", "confidence": 0.99}),
        json.dumps({"category": "maintenance", "confidence": 0.99, "unexpected": True}),
        json.dumps({"category": "maintenance", "confidence": 0.2}),
    ],
)
def test_qwen_classifier_invalid_unsupported_or_uncertain_fails_closed(monkeypatch, content):
    _configure(monkeypatch)
    _respond(monkeypatch, content)
    assert QwenClassifier().classify("tenant message") is None


def test_qwen_classifier_timeout_fails_closed(monkeypatch):
    _configure(monkeypatch)

    def timeout(*args, **kwargs):
        raise httpx.ReadTimeout("test timeout")

    monkeypatch.setattr(httpx.Client, "post", timeout)
    assert QwenClassifier().classify("tenant message") is None


def test_unconfigured_classifier_is_not_called_and_safe_mode_is_keyword(monkeypatch):
    for variable in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL"):
        monkeypatch.delenv(variable, raising=False)
    result = build_communication_classifier()
    assert isinstance(result, KeywordClassifier)


@pytest.mark.parametrize("failure", ["timeout", "connection", 429, 500, 503, "empty", "truncated"])
def test_qwen_classifier_recovers_once_from_transient_failure(monkeypatch, caplog, failure):
    _configure(monkeypatch)
    budgets = []

    def post(self, url, **kwargs):
        budgets.append(kwargs["json"]["max_completion_tokens"])
        if len(budgets) == 1:
            if failure == "timeout":
                raise httpx.ReadTimeout("sensitive exception must not be logged")
            if failure == "connection":
                raise httpx.ConnectError("sensitive exception must not be logged")
            if isinstance(failure, int):
                return httpx.Response(failure, request=httpx.Request("POST", url))
            choice = {"message": {"content": ""}, "finish_reason": "length" if failure == "truncated" else "stop"}
        else:
            choice = {"message": {"content": json.dumps({"category": "smalltalk", "confidence": 0.95})}}
        return httpx.Response(200, request=httpx.Request("POST", url), json={"choices": [choice]})

    monkeypatch.setattr(httpx.Client, "post", post)
    assert QwenClassifier().classify("hello").category is Category.SMALLTALK
    assert budgets == [160, 320]
    assert "attempt=1" in caplog.text
    assert "sensitive exception" not in caplog.text
    assert "test-only-key" not in caplog.text


def test_qwen_classifier_exhausted_timeouts_are_bounded_and_diagnosed(monkeypatch, caplog):
    _configure(monkeypatch)
    calls = []

    def timeout(*args, **kwargs):
        calls.append(1)
        raise httpx.ReadTimeout("private tenant content and key")

    monkeypatch.setattr(httpx.Client, "post", timeout)
    assert QwenClassifier().classify("hello") is None
    assert len(calls) == 2
    assert "code=timeout attempt=2" in caplog.text
    assert "private tenant content" not in caplog.text


@pytest.mark.parametrize("failure", [401, 403, "invalid", "unsupported", "uncertain"])
def test_qwen_classifier_does_not_retry_rejection_or_uncertainty(monkeypatch, caplog, failure):
    _configure(monkeypatch)
    calls = []

    def post(self, url, **kwargs):
        calls.append(1)
        if isinstance(failure, int):
            return httpx.Response(failure, request=httpx.Request("POST", url))
        content = {
            "invalid": "private invalid output",
            "unsupported": json.dumps({"category": "approve", "confidence": 1}),
            "uncertain": json.dumps({"category": "smalltalk", "confidence": 0.2}),
        }[failure]
        return httpx.Response(200, request=httpx.Request("POST", url),
                              json={"choices": [{"message": {"content": content}}]})

    monkeypatch.setattr(httpx.Client, "post", post)
    assert QwenClassifier().classify("private tenant message") is None
    assert len(calls) == 1
    code = "http_rejected" if isinstance(failure, int) else "low_confidence" if failure == "uncertain" else "invalid_response"
    assert "code=" + code in caplog.text
    assert "private" not in caplog.text


@pytest.mark.parametrize("timeout", ["0", "-1", "nan", "inf", "not-a-number"])
def test_qwen_classifier_invalid_timeout_fails_closed(monkeypatch, caplog, timeout):
    _configure(monkeypatch)
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", timeout)

    def unexpected_call(*args, **kwargs):
        pytest.fail("Invalid configuration must not call the model")

    monkeypatch.setattr(httpx.Client, "post", unexpected_call)
    assert QwenClassifier().classify("hello") is None
    assert "code=configuration" in caplog.text

