from dataclasses import replace

import httpx
import pytest

from app.config import SETTINGS
from app.services.truth.claim_decomposition import (
    ClaimDecomposer,
    deterministic_evidence_queries,
    local_decompose_claims,
)
from app.services.truth.pipeline import ClaimVerificationService


def _settings_with_reasoning_model(**overrides):
    return replace(SETTINGS, reasoning_model="some/openrouter-model", **overrides)


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


# --- local_decompose_claims (deterministic fallback) --------------------


def test_single_factual_sentence():
    claims = local_decompose_claims("The city has twelve public parks.")
    assert len(claims) == 1
    assert claims[0].claim_type == "factual"
    assert claims[0].verification_requirements == ["requires_supporting_or_contradicting_evidence"]


def test_multi_claim_input_is_decomposed_into_multiple_claims():
    text = "The city has twelve public parks. I think summer is the best season. Scientists say the lake froze in 1998."
    claims = local_decompose_claims(text)
    assert len(claims) == 3
    assert claims[0].claim_type == "factual"
    assert claims[1].claim_type == "opinion"
    assert claims[1].verification_requirements == ["not_independently_verifiable"]
    assert claims[2].claim_type == "scientific"


@pytest.mark.parametrize("text", ["", "   ", "\n\t "])
def test_empty_or_whitespace_input_yields_single_unverifiable_claim(text):
    claims = local_decompose_claims(text)
    assert len(claims) == 1
    assert claims[0].claim_type == "unverifiable"
    assert claims[0].verification_requirements == ["insufficient_checkable_content"]


def test_short_nonsense_sentence_is_unverifiable():
    claims = local_decompose_claims("abc.")
    assert claims[0].claim_type == "unverifiable"


def test_statistic_claim_type():
    claims = local_decompose_claims("Unemployment fell by 2.5 percent last quarter.")
    assert claims[0].claim_type == "statistic"


def test_prediction_claim_type_and_time_sensitive():
    claims = local_decompose_claims("The company will launch the product by 2030.")
    assert claims[0].claim_type == "prediction"
    assert claims[0].time_sensitive is True


def test_current_event_claim_type_and_time_sensitive():
    claims = local_decompose_claims("The mayor announced a new policy today.")
    assert claims[0].claim_type == "current_event"
    assert claims[0].time_sensitive is True


def test_quotation_claim_type():
    claims = local_decompose_claims('The spokesperson said "the bridge will reopen next month."')
    assert claims[0].claim_type == "quotation"


def test_historical_claim_type():
    claims = local_decompose_claims("The treaty was signed in the 19th century.")
    assert claims[0].claim_type == "historical"


def test_entities_extracted_and_sentence_initial_word_excluded():
    claims = local_decompose_claims("Marie Curie won the Nobel Prize in Stockholm.")
    entities = claims[0].entities
    assert "Marie Curie" not in entities  # sentence-initial phrase excluded by design
    assert "Nobel Prize" in entities


def test_location_extracted():
    claims = local_decompose_claims("The conference was held in Geneva last year.")
    assert claims[0].location == "Geneva"


def test_year_extracted_as_time_reference():
    claims = local_decompose_claims("The bridge was completed in 2004.")
    assert claims[0].time_reference == "2004"


def test_claim_ids_are_deterministic_and_stable():
    first = local_decompose_claims("The city has twelve public parks.")
    second = local_decompose_claims("The city has twelve public parks.")
    assert first[0].claim_id == second[0].claim_id


# --- deterministic_evidence_queries --------------------------------------


def test_evidence_query_for_factual_claim():
    claim = local_decompose_claims("The city has twelve public parks.")[0]
    assert deterministic_evidence_queries(claim) == ["The city has twelve public parks."]


def test_no_evidence_query_for_opinion_or_unverifiable_claims():
    opinion = local_decompose_claims("I think summer is the best season.")[0]
    unverifiable = local_decompose_claims("abc.")[0]
    assert deterministic_evidence_queries(opinion) == []
    assert deterministic_evidence_queries(unverifiable) == []


# --- ClaimDecomposer (OpenRouter routing + fallback) ---------------------


def test_decomposer_not_configured_without_api_key(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    decomposer = ClaimDecomposer(_settings_with_reasoning_model())
    assert decomposer.configured is False


def test_decomposer_not_configured_without_reasoning_model(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    decomposer = ClaimDecomposer(replace(SETTINGS, reasoning_model=None))
    assert decomposer.configured is False


def test_decomposer_configured_requires_both(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    decomposer = ClaimDecomposer(_settings_with_reasoning_model())
    assert decomposer.configured is True


def test_decompose_falls_back_when_not_configured(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    decomposer = ClaimDecomposer(replace(SETTINGS, reasoning_model=None))
    claims = decomposer.decompose("The city has twelve public parks.")
    assert claims == local_decompose_claims("The city has twelve public parks.")


def test_decompose_falls_back_on_openrouter_network_failure(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    decomposer = ClaimDecomposer(_settings_with_reasoning_model())

    def fake_post(url, **kwargs):
        raise httpx.ConnectError("no network in tests", request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)
    claims = decomposer.decompose("The city has twelve public parks.")
    assert claims == local_decompose_claims("The city has twelve public parks.")


def test_decompose_falls_back_on_malformed_openrouter_response(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    decomposer = ClaimDecomposer(_settings_with_reasoning_model())

    def fake_post(url, **kwargs):
        return _FakeResponse(_chat_payload("not valid json"))

    monkeypatch.setattr(httpx, "post", fake_post)
    claims = decomposer.decompose("The city has twelve public parks.")
    assert claims == local_decompose_claims("The city has twelve public parks.")


def test_decompose_falls_back_when_model_returns_zero_claims(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    decomposer = ClaimDecomposer(_settings_with_reasoning_model())

    def fake_post(url, **kwargs):
        # Valid JSON, schema-conformant, but an empty list -- exercises the
        # explicit "claims or fallback" guard rather than the malformed-
        # response path.
        return _FakeResponse(_chat_payload('{"claims": []}'))

    monkeypatch.setattr(httpx, "post", fake_post)
    claims = decomposer.decompose("The city has twelve public parks.")
    assert claims == local_decompose_claims("The city has twelve public parks.")


def test_decompose_uses_openrouter_output_when_configured(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    decomposer = ClaimDecomposer(_settings_with_reasoning_model())

    content = (
        '{"claims": [{"original_text": "The city has twelve public parks.", '
        '"normalized_claim": "the city has twelve public parks", "claim_type": "factual", '
        '"entities": ["City Parks Department"], "location": null, "time_reference": null, '
        '"time_sensitive": false, "verification_requirements": ["requires_supporting_or_contradicting_evidence"]}]}'
    )

    def fake_post(url, **kwargs):
        return _FakeResponse(_chat_payload(content))

    monkeypatch.setattr(httpx, "post", fake_post)
    claims = decomposer.decompose("The city has twelve public parks.")
    assert len(claims) == 1
    assert claims[0].normalized_claim == "the city has twelve public parks"
    assert claims[0].entities == ["City Parks Department"]


def test_decompose_handles_multi_claim_model_output(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    decomposer = ClaimDecomposer(_settings_with_reasoning_model())

    content = (
        '{"claims": ['
        '{"original_text": "A", "normalized_claim": "claim a", "claim_type": "factual", '
        '"entities": [], "location": null, "time_reference": null, "time_sensitive": false, '
        '"verification_requirements": ["requires_supporting_or_contradicting_evidence"]},'
        '{"original_text": "B", "normalized_claim": "claim b", "claim_type": "opinion", '
        '"entities": [], "location": null, "time_reference": null, "time_sensitive": false, '
        '"verification_requirements": ["not_independently_verifiable"]}'
        ']}'
    )

    def fake_post(url, **kwargs):
        return _FakeResponse(_chat_payload(content))

    monkeypatch.setattr(httpx, "post", fake_post)
    claims = decomposer.decompose("A. B.")
    assert [c.normalized_claim for c in claims] == ["claim a", "claim b"]
    assert claims[0].claim_id != claims[1].claim_id


# --- wiring into ClaimVerificationService --------------------------------


def test_service_constructs_a_claim_decomposer_by_default(tmp_path):
    settings = replace(SETTINGS, cache_path=tmp_path / "cache.sqlite3")
    service = ClaimVerificationService(settings)
    assert isinstance(service.claim_decomposer, ClaimDecomposer)


def test_service_accepts_claim_decomposer_override(tmp_path):
    settings = replace(SETTINGS, cache_path=tmp_path / "cache.sqlite3")
    custom = ClaimDecomposer(settings)
    service = ClaimVerificationService(settings, claim_decomposer=custom)
    assert service.claim_decomposer is custom
