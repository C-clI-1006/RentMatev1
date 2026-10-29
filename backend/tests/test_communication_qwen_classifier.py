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

