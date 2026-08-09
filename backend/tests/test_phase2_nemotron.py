from dataclasses import replace

import httpx
import pytest

from app.config import SETTINGS
from app.models import GeminiClaimExtraction, GeminiEvidenceAssessment
from app.services.truth.nemotron_verifier import NemotronVerifier
from app.services.truth.openrouter_client import (
    OpenRouterClient,
    OpenRouterError,
    OpenRouterMalformedResponse,
    OpenRouterTimeout,
)
from app.services.truth.pipeline import create_verifier
from app.services.truth.verifier import GeminiEvidenceVerifier


def _settings_with_openrouter(**overrides):
    return replace(SETTINGS, verifier_model="some/openrouter-model", **overrides)


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=httpx.Request("POST", "https://example.invalid"), response=httpx.Response(self.status_code))

    def json(self):
        return self._payload


def _chat_payload(content: str) -> dict:
    return {"choices": [{"message": {"content": content}}]}


# --- OpenRouterClient -------------------------------------------------


def test_openrouter_client_never_calls_network_when_mocked_success(monkeypatch):
    """Sanity check for the harness itself: httpx.post is intercepted, not real."""
    called = {}

    def fake_post(url, **kwargs):
        called["url"] = url
        called["json"] = kwargs["json"]
        return _FakeResponse(_chat_payload('{"kind": "factual", "main_claim": "The sky is blue.", "multiple_claims_detected": false}'))

    monkeypatch.setattr(httpx, "post", fake_post)

    client = OpenRouterClient(_settings_with_openrouter(), api_key="test-key")
    result = client.complete_json(
        model="some/openrouter-model",
        system="system prompt",
        user="user prompt",
        response_schema=GeminiClaimExtraction,
        timeout=1.0,
    )

    assert isinstance(result, GeminiClaimExtraction)
    assert result.kind == "factual"
    assert called["url"].endswith("/chat/completions")
    assert called["json"]["model"] == "some/openrouter-model"


def test_openrouter_client_timeout_is_structured(monkeypatch):
    def fake_post(url, **kwargs):
        raise httpx.TimeoutException("slow", request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)
    client = OpenRouterClient(_settings_with_openrouter(), api_key="test-key")

    with pytest.raises(OpenRouterTimeout):
        client.complete_json(model="m", system="s", user="u", response_schema=GeminiClaimExtraction, timeout=1.0)


def test_openrouter_client_http_error_is_structured(monkeypatch):
    def fake_post(url, **kwargs):
        raise httpx.ConnectError("refused", request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)
    client = OpenRouterClient(_settings_with_openrouter(), api_key="test-key")

    with pytest.raises(OpenRouterError):
        client.complete_json(model="m", system="s", user="u", response_schema=GeminiClaimExtraction, timeout=1.0)


def test_openrouter_client_malformed_json_is_structured(monkeypatch):
    def fake_post(url, **kwargs):
        return _FakeResponse(_chat_payload("not valid json"))

    monkeypatch.setattr(httpx, "post", fake_post)
    client = OpenRouterClient(_settings_with_openrouter(), api_key="test-key")

    with pytest.raises(OpenRouterMalformedResponse):
        client.complete_json(model="m", system="s", user="u", response_schema=GeminiClaimExtraction, timeout=1.0)


def test_openrouter_client_schema_mismatch_is_structured(monkeypatch):
    def fake_post(url, **kwargs):
        return _FakeResponse(_chat_payload('{"unexpected": "shape"}'))

    monkeypatch.setattr(httpx, "post", fake_post)
    client = OpenRouterClient(_settings_with_openrouter(), api_key="test-key")

    with pytest.raises(OpenRouterMalformedResponse):
        client.complete_json(model="m", system="s", user="u", response_schema=GeminiClaimExtraction, timeout=1.0)


def test_openrouter_client_unexpected_response_shape_is_structured(monkeypatch):
    def fake_post(url, **kwargs):
        return _FakeResponse({"choices": []})

    monkeypatch.setattr(httpx, "post", fake_post)
    client = OpenRouterClient(_settings_with_openrouter(), api_key="test-key")

    with pytest.raises(OpenRouterMalformedResponse):
        client.complete_json(model="m", system="s", user="u", response_schema=GeminiClaimExtraction, timeout=1.0)


def test_openrouter_base_url_defaults_and_is_overridable():
    default_client = OpenRouterClient(_settings_with_openrouter(), api_key="k")
    assert default_client.base_url == "https://openrouter.ai/api/v1"

    overridden = OpenRouterClient(_settings_with_openrouter(openrouter_base_url="https://custom.internal/v9/"), api_key="k")
    assert overridden.base_url == "https://custom.internal/v9"


# --- NemotronVerifier ---------------------------------------------------


def test_nemotron_not_configured_without_api_key(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    verifier = NemotronVerifier(_settings_with_openrouter())
    assert verifier.configured is False


def test_nemotron_not_configured_without_model(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    verifier = NemotronVerifier(replace(SETTINGS, verifier_model=None))
    assert verifier.configured is False


def test_nemotron_configured_requires_both_key_and_model(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    verifier = NemotronVerifier(_settings_with_openrouter())
    assert verifier.configured is True


def test_nemotron_extract_claim_falls_back_when_not_configured(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    verifier = NemotronVerifier(replace(SETTINGS, verifier_model=None))
    result = verifier.extract_claim("The city has twelve public parks.")
    assert result.kind == "factual"
    assert result.main_claim == "The city has twelve public parks."


def test_nemotron_extract_claim_falls_back_on_openrouter_failure(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    verifier = NemotronVerifier(_settings_with_openrouter())

    def fake_post(url, **kwargs):
        raise httpx.ConnectError("no network in tests", request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)
    result = verifier.extract_claim("The city has twelve public parks.")
    assert result.kind == "factual"
    assert result.main_claim == "The city has twelve public parks."


def test_nemotron_extract_claim_uses_openrouter_when_configured(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    verifier = NemotronVerifier(_settings_with_openrouter())

    def fake_post(url, **kwargs):
        return _FakeResponse(_chat_payload('{"kind": "opinion", "main_claim": "Summer is the best season.", "multiple_claims_detected": false}'))

    monkeypatch.setattr(httpx, "post", fake_post)
    result = verifier.extract_claim("I think summer is the best season.")
    assert result.kind == "opinion"
    assert result.main_claim == "Summer is the best season."


def test_nemotron_compare_raises_when_not_configured(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    verifier = NemotronVerifier(replace(SETTINGS, verifier_model=None))
    with pytest.raises(RuntimeError):
        verifier.compare("claim", [])


def test_nemotron_compare_returns_assessment_when_configured(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    verifier = NemotronVerifier(_settings_with_openrouter())

    def fake_post(url, **kwargs):
        return _FakeResponse(
            _chat_payload('{"verdict": "supported", "confidence": "high", "summary": "Backed by one source.", "source_stances": ["supports"]}')
        )

    monkeypatch.setattr(httpx, "post", fake_post)

    class _StubRanked:
        def __init__(self):
            class _E:
                title = "Title"
                url = "https://example.org/a"
                published_at = None
            self.evidence = _E()
            self.passages = ["a passage"]

    assessment = verifier.compare("claim", [_StubRanked()])
    assert isinstance(assessment, GeminiEvidenceAssessment)
    assert assessment.verdict == "supported"


def test_nemotron_compare_rejects_mismatched_stance_count(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    verifier = NemotronVerifier(_settings_with_openrouter())

    def fake_post(url, **kwargs):
        return _FakeResponse(
            _chat_payload('{"verdict": "supported", "confidence": "high", "summary": "x", "source_stances": ["supports", "supports"]}')
        )

    monkeypatch.setattr(httpx, "post", fake_post)

    class _StubRanked:
        def __init__(self):
            class _E:
                title = "Title"
                url = "https://example.org/a"
                published_at = None
            self.evidence = _E()
            self.passages = ["a passage"]

    with pytest.raises(ValueError):
        verifier.compare("claim", [_StubRanked()])


# --- create_verifier selection ------------------------------------------


def test_create_verifier_defaults_to_gemini_when_openrouter_unset(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    verifier = create_verifier(replace(SETTINGS, verifier_model=None))
    assert isinstance(verifier, GeminiEvidenceVerifier)


def test_create_verifier_selects_nemotron_when_fully_configured(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    verifier = create_verifier(_settings_with_openrouter())
    assert isinstance(verifier, NemotronVerifier)
    assert verifier.configured is True


def test_create_verifier_stays_on_gemini_if_only_key_set(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    verifier = create_verifier(replace(SETTINGS, verifier_model=None))
    assert isinstance(verifier, GeminiEvidenceVerifier)


def test_create_verifier_stays_on_gemini_if_only_model_set(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    verifier = create_verifier(_settings_with_openrouter())
    assert isinstance(verifier, GeminiEvidenceVerifier)
