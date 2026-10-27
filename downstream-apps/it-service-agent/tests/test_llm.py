import json

import httpx
import pytest
from fastapi.testclient import TestClient

from it_app.llm import Advisor, ModelError
from it_app.main import create_app


SOURCES = [{"id": "SOP-NET-001", "content": "Check Wi-Fi and contact IT", "version": 1}]


@pytest.fixture
def model_env(monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "https://model.invalid/v1")
    monkeypatch.setenv("LLM_API_KEY", "test-key-do-not-leak")
    monkeypatch.setenv("LLM_MODEL", "test-model")


def test_explicit_model_adapter_uses_http_and_citations(model_env):
    requests = []

    def respond(request):
        requests.append(request)
        body = json.loads(request.content)
        assert body["model"] == "test-model"
        assert request.url.path == "/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer test-key-do-not-leak"
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({"advice": "Check Wi-Fi", "citation_ids": ["SOP-NET-001"], "owner": "bob", "action": "delete_files"})}}]})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        result = Advisor("llm", client).suggest("Wi-Fi unavailable", SOURCES)
    assert len(requests) == 1
    assert result == {"advice": "Check Wi-Fi", "citation_ids": ["SOP-NET-001"]}


@pytest.mark.parametrize("content", ["not json", "[]", '{"advice":"do it","citation_ids":["INVENTED"]}', '{"advice":"do it","citation_ids":[]}'])
def test_invalid_model_output_is_not_a_success(model_env, content):
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"choices": [{"message": {"content": content}}]}))) as client:
        with pytest.raises(ModelError):
            Advisor("llm", client).suggest("Wi-Fi unavailable", SOURCES)


def test_model_configuration_requires_explicit_credentials(monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    with pytest.raises(ValueError, match="LLM_BASE_URL"):
        Advisor("llm")


def test_invalid_model_url_is_reported_as_model_failure(model_env, monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "http://[invalid")
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={}))) as client:
        with pytest.raises(ModelError):
            Advisor("llm", client).suggest("Wi-Fi unavailable", SOURCES)


def test_offline_mode_does_not_make_network_call():
    def forbidden(request):
        raise AssertionError("offline mode must not use the network")

    with httpx.Client(transport=httpx.MockTransport(forbidden)) as client:
        result = Advisor("offline", client).suggest("Wi-Fi unavailable", SOURCES)
    assert result["advice"] == SOURCES[0]["content"]


def test_model_failure_stops_before_approval_and_can_retry(tmp_path, model_env):
    attempts = []

    def respond(request):
        attempts.append(1)
        if len(attempts) == 1:
            return httpx.Response(503, text="secret-provider-details")
        content = {"advice": "Check Wi-Fi; ask IT if unresolved", "citation_ids": ["SOP-NET-001"]}
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(content)}}]})

    with httpx.Client(transport=httpx.MockTransport(respond)) as model_client:
        with TestClient(create_app(tmp_path / "it.db", model_mode="llm", llm_client=model_client)) as client:
            headers = {"Authorization": "Bearer demo-alice"}
            response = client.post("/api/runs", headers=headers, json={"description": "Wi-Fi unavailable", "device_id": "LAPTOP-A01"})
            assert response.status_code == 201
            failed = response.json()
            assert failed["status"] == "model_failed"
            assert failed["proposal"] is None
            assert failed["pending"] == []
            assert failed["ticket"] is None
            assert "secret" not in failed["error"]
            retry = client.post(f"/api/runs/{failed['run_id']}/retry", headers=headers)
            assert retry.status_code == 200, retry.text
            assert retry.json()["status"] == "waiting_approval"
            assert retry.json()["model_mode"] == "llm"
            assert retry.json()["ticket"] is None
