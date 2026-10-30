"""Local operator configuration: authorization, secret handling and adapter wiring."""
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from llm import configuration
from llm.classifier import QwenClassifier
from llm.classify import ClassifyFailure, QwenDocumentClassifier, build_document_classifier
from main import create_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(configuration, "_runtime_configuration", None)
    monkeypatch.setenv("LLM_BASE_URL", "")
    monkeypatch.setenv("LLM_API_KEY", "")
    monkeypatch.setenv("LLM_MODEL", "")
    monkeypatch.setenv("RENTMATE_SCHEDULER_TOKEN", "operator-test-token")
    app = create_app(str(tmp_path / "settings.db"), str(tmp_path / "files"), evidence_dispatch_managers={})
    try:
        with TestClient(app, client=("127.0.0.1", 50000)) as session:
            yield session
    finally:
        app.state.evidence_service._repo.close()


AUTH = {"Authorization": "Bearer operator-test-token"}
SETTINGS = {"base_url": "http://model.example.test/v1", "model": "test-model", "api_key": "synthetic-key-not-a-real-secret"}


def test_model_settings_require_local_operator_not_tenant_or_manager(client):
    assert client.get("/api/ops/llm/settings").status_code == 401
    assert client.get("/api/ops/llm/settings", headers={"Authorization": "Bearer manager-demo"}).status_code == 401
    assert client.get("/api/ops/llm/settings", headers={"Authorization": "Bearer tenant-demo"}).status_code == 401
    assert client.get("/api/ops/llm/settings", headers=AUTH).status_code == 200
    with TestClient(client.app, client=("192.0.2.1", 50000)) as remote:
        assert remote.get("/api/ops/llm/settings", headers=AUTH).status_code == 403
    assert client.put("/api/ops/llm/settings", headers={**AUTH, "Origin": "https://untrusted.example"}, json=SETTINGS).status_code == 403


def test_apply_settings_never_reads_back_key_or_logs_it(client, caplog, capsys):
    response = client.put("/api/ops/llm/settings", headers=AUTH, json=SETTINGS)
    assert response.status_code == 200
    assert response.json() == {
        "base_url": SETTINGS["base_url"], "model": SETTINGS["model"],
        "api_key_configured": True, "configured": True,
        "source": "runtime", "persistence": "server_process_only",
    }
    assert response.headers["cache-control"] == "no-store"
    read = client.get("/api/ops/llm/settings", headers=AUTH)
    assert SETTINGS["api_key"] not in response.text + read.text + caplog.text + capsys.readouterr().out
    assert SETTINGS["api_key"] not in repr(configuration.get_configuration())
    classifier = QwenClassifier()
    assert classifier.configured
    assert classifier._model == SETTINGS["model"]
    assert classifier._base_url == SETTINGS["base_url"]


@pytest.mark.parametrize("changes", [
    {"base_url": "file:///etc/passwd"},
    {"base_url": "http://user:password@model.test/v1"},
    {"base_url": "http://model.test/v1?key=secret"},
    {"base_url": 7}, {"model": ""}, {"api_key": ""},
])
def test_invalid_settings_errors_are_sanitized_and_do_not_apply(client, changes):
    response = client.put("/api/ops/llm/settings", headers=AUTH, json={**SETTINGS, **changes})
    assert response.status_code == 422
    assert SETTINGS["api_key"] not in response.text
    assert client.get("/api/ops/llm/settings", headers=AUTH).json()["configured"] is False


def test_blank_key_can_keep_same_destination_but_not_forward_to_new_address(client):
    assert client.put("/api/ops/llm/settings", headers=AUTH, json=SETTINGS).status_code == 200
    same = {**SETTINGS, "api_key": "", "model": "another-model"}
    assert client.put("/api/ops/llm/settings", headers=AUTH, json=same).status_code == 200
    changed = {**same, "base_url": "http://different.example.test/v1"}
    assert client.put("/api/ops/llm/settings", headers=AUTH, json=changed).status_code == 422
    assert configuration.get_configuration().base_url == SETTINGS["base_url"]
    restored = client.delete("/api/ops/llm/settings", headers=AUTH)
    assert restored.status_code == 200
    assert restored.json()["configured"] is False
    assert restored.json()["source"] == "environment"


def test_existing_evidence_adapter_uses_updated_settings_on_next_call(client, monkeypatch):
    adapter = build_document_classifier()  # Created before the operator applies settings.
    captured = []

    def classify(self, **kwargs):
        captured.append((self._base_url, self._model, self._api_key))
        return ClassifyFailure("timeout", "Synthetic transport boundary")

    monkeypatch.setattr(QwenDocumentClassifier, "classify", classify)
    client.put("/api/ops/llm/settings", headers=AUTH, json=SETTINGS)
    adapter.classify(text="Invoice", filename="invoice.pdf", file_type="pdf")
    assert captured == [(SETTINGS["base_url"], SETTINGS["model"], SETTINGS["api_key"])]


def test_model_probe_uses_configured_adapter_and_never_echoes_upstream_secrets(client, monkeypatch):
    assert client.post("/api/ops/llm/test", headers=AUTH).status_code == 409
    client.put("/api/ops/llm/settings", headers=AUTH, json=SETTINGS)
    real_client = httpx.Client
    requests = []

    def answer(request):
        requests.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({"category": "smalltalk", "confidence": 0.98})}}]})

    monkeypatch.setattr("llm.classifier.httpx.Client", lambda **kwargs: real_client(transport=httpx.MockTransport(answer), **kwargs))
    result = client.post("/api/ops/llm/test", headers=AUTH)
    assert result.status_code == 200
    assert result.json()["status"] == "validated_model_response"
    assert result.json()["source"] == "llm"
    assert str(requests[0].url) == SETTINGS["base_url"] + "/chat/completions"
    assert json.loads(requests[0].content)["model"] == SETTINGS["model"]
    assert SETTINGS["api_key"] not in result.text

    def bad_answer(request):
        return httpx.Response(401, text=SETTINGS["api_key"])

    monkeypatch.setattr("llm.classifier.httpx.Client", lambda **kwargs: real_client(transport=httpx.MockTransport(bad_answer), **kwargs))
    failure = client.post("/api/ops/llm/test", headers=AUTH)
    assert failure.status_code == 502
    assert SETTINGS["api_key"] not in failure.text
