from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import SETTINGS
from app.main import create_app
from app.services.truth.openrouter_client import (
    OpenRouterClient,
    OpenRouterError,
    OpenRouterMalformedResponse,
    OpenRouterTimeout,
    ToolCallResult,
)
from app.services.truth.quick_ask import UNAVAILABLE_MESSAGE, QuickAskService


def _settings_with_classifier(**overrides):
    return replace(SETTINGS, classifier_model="google/gemma-4-26b-a4b-it:free", **overrides)


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=httpx.Request("POST", "https://example.invalid"), response=httpx.Response(self.status_code))

    def json(self):
        return self._payload


def _chat_payload(content: str, annotations=None) -> dict:
    message = {"content": content}
    if annotations is not None:
        message["annotations"] = annotations
    return {"choices": [{"message": message}]}


# --- OpenRouterClient.complete_with_tools --------------------------------


def test_complete_with_tools_sends_tools_array(monkeypatch):
    captured = {}

    def fake_post(url, **kwargs):
        captured["json"] = kwargs["json"]
        return _FakeResponse(_chat_payload("The sky is blue due to Rayleigh scattering."))

    monkeypatch.setattr(httpx, "post", fake_post)
    client = OpenRouterClient(_settings_with_classifier(), api_key="test-key")

    result = client.complete_with_tools(
        model="google/gemma-4-26b-a4b-it:free",
        system="system prompt",
        user="Why is the sky blue?",
        tools=[{"type": "openrouter:web_search"}, {"type": "openrouter:web_fetch"}],
        timeout=1.0,
    )

    assert isinstance(result, ToolCallResult)
    assert result.content == "The sky is blue due to Rayleigh scattering."
    assert result.citations == []
    assert captured["json"]["tools"] == [{"type": "openrouter:web_search"}, {"type": "openrouter:web_fetch"}]
    assert "response_format" not in captured["json"]


def test_complete_with_tools_extracts_nested_url_citation_annotations(monkeypatch):
    annotations = [
        {"type": "url_citation", "url_citation": {"url": "https://example.org/a", "title": "A"}},
        {"type": "url_citation", "url_citation": {"url": "https://example.org/b", "title": "B"}},
    ]

    def fake_post(url, **kwargs):
        return _FakeResponse(_chat_payload("Answer with sources.", annotations))

    monkeypatch.setattr(httpx, "post", fake_post)
    client = OpenRouterClient(_settings_with_classifier(), api_key="test-key")
    result = client.complete_with_tools(model="m", system="s", user="u", tools=[], timeout=1.0)

    assert result.citations == ["https://example.org/a", "https://example.org/b"]


def test_complete_with_tools_extracts_flat_url_citation_annotations(monkeypatch):
    """Defensive fallback shape (annotation.url directly, not nested under
    annotation.url_citation) -- the exact wire shape isn't fully pinned
    down in public docs, so both plausible shapes are covered."""
    annotations = [{"type": "url_citation", "url": "https://example.org/flat"}]

    def fake_post(url, **kwargs):
        return _FakeResponse(_chat_payload("Answer.", annotations))

    monkeypatch.setattr(httpx, "post", fake_post)
    client = OpenRouterClient(_settings_with_classifier(), api_key="test-key")
    result = client.complete_with_tools(model="m", system="s", user="u", tools=[], timeout=1.0)

    assert result.citations == ["https://example.org/flat"]


def test_complete_with_tools_ignores_non_url_citation_and_duplicate_annotations():
    annotations = [
        {"type": "something_else", "url": "https://example.org/ignored"},
        {"type": "url_citation", "url_citation": {"url": "https://example.org/dup"}},
        {"type": "url_citation", "url_citation": {"url": "https://example.org/dup"}},
        {"type": "url_citation", "url_citation": {"url": "javascript:alert(1)"}},
        "not-a-dict",
    ]
    from app.services.truth.openrouter_client import _extract_citations

    citations = _extract_citations({"content": "x", "annotations": annotations})
    assert citations == ["https://example.org/dup"]


def test_complete_with_tools_missing_annotations_returns_no_citations(monkeypatch):
    def fake_post(url, **kwargs):
        return _FakeResponse(_chat_payload("Answer with no annotations field at all."))

    monkeypatch.setattr(httpx, "post", fake_post)
    client = OpenRouterClient(_settings_with_classifier(), api_key="test-key")
    result = client.complete_with_tools(model="m", system="s", user="u", tools=[], timeout=1.0)
    assert result.citations == []


def test_complete_with_tools_timeout_is_structured(monkeypatch):
    def fake_post(url, **kwargs):
        raise httpx.TimeoutException("slow", request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)
    client = OpenRouterClient(_settings_with_classifier(), api_key="test-key")
    with pytest.raises(OpenRouterTimeout):
        client.complete_with_tools(model="m", system="s", user="u", tools=[], timeout=1.0)


def test_complete_with_tools_http_error_is_structured(monkeypatch):
    def fake_post(url, **kwargs):
        raise httpx.ConnectError("refused", request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)
    client = OpenRouterClient(_settings_with_classifier(), api_key="test-key")
    with pytest.raises(OpenRouterError):
        client.complete_with_tools(model="m", system="s", user="u", tools=[], timeout=1.0)


def test_complete_with_tools_missing_content_is_malformed(monkeypatch):
    def fake_post(url, **kwargs):
        return _FakeResponse({"choices": [{"message": {}}]})

    monkeypatch.setattr(httpx, "post", fake_post)
    client = OpenRouterClient(_settings_with_classifier(), api_key="test-key")
    with pytest.raises(OpenRouterMalformedResponse):
        client.complete_with_tools(model="m", system="s", user="u", tools=[], timeout=1.0)


def test_complete_with_tools_empty_choices_is_malformed(monkeypatch):
    def fake_post(url, **kwargs):
        return _FakeResponse({"choices": []})

    monkeypatch.setattr(httpx, "post", fake_post)
    client = OpenRouterClient(_settings_with_classifier(), api_key="test-key")
    with pytest.raises(OpenRouterMalformedResponse):
        client.complete_with_tools(model="m", system="s", user="u", tools=[], timeout=1.0)


# --- QuickAskService ------------------------------------------------------


def test_quick_ask_not_configured_without_api_key(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    service = QuickAskService(_settings_with_classifier())
    assert service.configured is False


def test_quick_ask_not_configured_without_model(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    service = QuickAskService(replace(SETTINGS, classifier_model=None))
    assert service.configured is False


def test_quick_ask_returns_unavailable_message_when_not_configured(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    service = QuickAskService(replace(SETTINGS, classifier_model=None))
    result = service.ask("Is the moon made of cheese?")
    assert result.available is False
    assert result.answer == UNAVAILABLE_MESSAGE
    assert result.sources == []
    assert result.model is None


def test_quick_ask_returns_unavailable_message_on_openrouter_failure(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

    class FailingClient:
        def complete_with_tools(self, **kwargs):
            raise OpenRouterError("boom")

    service = QuickAskService(_settings_with_classifier(), client=FailingClient())
    result = service.ask("Is the moon made of cheese?")
    assert result.available is False
    assert result.answer == UNAVAILABLE_MESSAGE
    assert result.sources == []


def test_quick_ask_returns_model_answer_and_sources_when_configured(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

    class StubClient:
        def __init__(self):
            self.calls = []

        def complete_with_tools(self, *, model, system, user, tools, timeout):
            self.calls.append({"model": model, "user": user, "tools": tools})
            return ToolCallResult(content="No, the moon is made of rock.", citations=["https://example.org/moon"])

    stub = StubClient()
    service = QuickAskService(_settings_with_classifier(), client=stub)
    result = service.ask("Is the moon made of cheese?")

    assert result.available is True
    assert result.answer == "No, the moon is made of rock."
    assert result.sources == ["https://example.org/moon"]
    assert result.model == "google/gemma-4-26b-a4b-it:free"
    assert stub.calls[0]["user"] == "Is the moon made of cheese?"
    assert {t["type"] for t in stub.calls[0]["tools"]} == {"openrouter:web_search", "openrouter:web_fetch"}


# --- /api/v2/quick-ask endpoint -------------------------------------------


def test_quick_ask_endpoint_returns_unavailable_when_not_configured():
    service = QuickAskService(replace(SETTINGS, classifier_model=None))
    with TestClient(create_app(quick_ask_service=service)) as client:
        response = client.post("/api/v2/quick-ask", json={"text": "Is the moon made of cheese?"})

    assert response.status_code == 200
    body = response.json()
    assert body["available"] is False
    assert body["answer"] == UNAVAILABLE_MESSAGE
    assert body["sources"] == []


def test_quick_ask_endpoint_returns_answer_when_configured(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

    class StubClient:
        def complete_with_tools(self, **kwargs):
            return ToolCallResult(content="Direct answer.", citations=["https://example.org/x"])

    service = QuickAskService(_settings_with_classifier(), client=StubClient())
    with TestClient(create_app(quick_ask_service=service)) as client:
        response = client.post("/api/v2/quick-ask", json={"text": "A quick question."})

    assert response.status_code == 200
    body = response.json()
    assert body["available"] is True
    assert body["answer"] == "Direct answer."
    assert body["sources"] == ["https://example.org/x"]
    assert body["model"] == "google/gemma-4-26b-a4b-it:free"


def test_quick_ask_endpoint_rejects_empty_text():
    service = QuickAskService(replace(SETTINGS, classifier_model=None))
    with TestClient(create_app(quick_ask_service=service)) as client:
        response = client.post("/api/v2/quick-ask", json={"text": ""})
    assert response.status_code == 422


def test_quick_ask_endpoint_rejects_extra_fields():
    service = QuickAskService(replace(SETTINGS, classifier_model=None))
    with TestClient(create_app(quick_ask_service=service)) as client:
        response = client.post("/api/v2/quick-ask", json={"text": "hi", "language": "en"})
    assert response.status_code == 422
