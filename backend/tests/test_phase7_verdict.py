from dataclasses import replace
from datetime import datetime, timezone
from typing import get_args

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import SETTINGS
from app.main import create_app
from app.services.truth.claim_decomposition import local_decompose_claims
from app.services.truth.domain import ClaimEvidenceMatrix, Evidence, InternalVerdict
from app.services.truth.evidence_matrix import GAP_STALE_TIME_SENSITIVE
from app.services.truth.evidence_processing import hash_evidence_content
from app.services.truth.nemotron_verifier import NemotronVerifier
from app.services.truth.verdict_generation import generate_verdict, heuristic_verdict, to_public_verdict
from app.services.truth.verifier import GeminiEvidenceVerifier
from app.services.truth.verify_v2 import MultiClaimVerificationService


# Credential-safety fixture now lives in tests/conftest.py (session-wide autouse).
# Tests below that want the "configured" path use their own monkeypatch.setenv(...),
# which overrides the conftest fixture's delenv for that test.


RETRIEVED_AT = datetime(2026, 8, 9, tzinfo=timezone.utc)


def _claim(text="The city has twelve public parks."):
    return local_decompose_claims(text)[0]


def _evidence(claim_id, url, text):
    return Evidence(
        evidence_id=f"evidence_{hash_evidence_content(url + text)[:24]}",
        claim_id=claim_id,
        url=url,
        canonical_url=url,
        publisher="Example Institute",
        author=None,
        title="Title",
        published_at=None,
        retrieved_at=RETRIEVED_AT,
        source_type="webpage",
        primary_or_secondary=None,
        text=text,
        evidence_chunk=text,
        provider="stub",
        content_hash=hash_evidence_content(text),
        metadata={},
    )


def _matrix(claim, *, supporting=None, contradicting=None, context=None, unresolved=None):
    return ClaimEvidenceMatrix(
        claim=claim,
        supporting=supporting or [],
        contradicting=contradicting or [],
        context=context or [],
        unresolved=unresolved or [],
    )


class StubVerificationModel:
    """Duck-typed VerificationModel, mirroring test_truth.py's StubVerifier
    pattern -- avoids needing to mock the Gemini SDK or OpenRouter for pure
    orchestration tests (generate_verdict's selection/fallback logic)."""

    def __init__(self, *, configured=True, result=None, raises=None):
        self._configured = configured
        self.result = result
        self.raises = raises
        self.assess_matrix_calls = 0

    @property
    def configured(self):
        return self._configured

    def extract_claim(self, text):
        raise NotImplementedError

    def compare(self, claim, evidence):
        raise NotImplementedError

    def assess_matrix(self, claim, matrix, *, rounds):
        self.assess_matrix_calls += 1
        if self.raises:
            raise self.raises
        return self.result


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _chat_payload(content: str) -> dict:
    return {"choices": [{"message": {"content": content}}]}


# --- to_public_verdict mapping table ---------------------------------------


@pytest.mark.parametrize(
    "internal,public",
    [
        ("SUPPORTED", "TRUE"),
        ("CONTRADICTED", "FALSE"),
        ("MISLEADING", "MISLEADING"),
        ("MISSING_CONTEXT", "MISLEADING"),
        ("DISPUTED", "INSUFFICIENT_EVIDENCE"),
        ("OUTDATED", "INSUFFICIENT_EVIDENCE"),
        ("INSUFFICIENT_EVIDENCE", "INSUFFICIENT_EVIDENCE"),
        ("UNVERIFIABLE", "INSUFFICIENT_EVIDENCE"),
    ],
)
def test_public_verdict_mapping_matches_handoff_doc_section_10_5(internal: InternalVerdict, public):
    assert to_public_verdict(internal) == public


def test_mapping_table_covers_every_internal_verdict_value():
    # Derives the expected set from the actual InternalVerdict Literal
    # rather than hardcoding it twice -- catches drift if that type is
    # ever extended without updating to_public_verdict's mapping.
    for value in get_args(InternalVerdict):
        assert to_public_verdict(value) in ("TRUE", "FALSE", "MISLEADING", "INSUFFICIENT_EVIDENCE")


# --- deterministic heuristic verdict ---------------------------------------


def test_opinion_claim_is_unverifiable():
    claim = local_decompose_claims("I think summer is the best season.")[0]
    result = heuristic_verdict(claim, _matrix(claim), rounds=1)
    assert result.verdict == "UNVERIFIABLE"


def test_no_evidence_is_insufficient():
    claim = _claim()
    result = heuristic_verdict(claim, _matrix(claim, unresolved=["no evidence retrieved for this claim"]), rounds=1)
    assert result.verdict == "INSUFFICIENT_EVIDENCE"


def test_supporting_only_is_supported():
    claim = _claim()
    supporting = [_evidence(claim.claim_id, "https://a.example/1", "supports it")]
    result = heuristic_verdict(claim, _matrix(claim, supporting=supporting), rounds=1)
    assert result.verdict == "SUPPORTED"
    assert to_public_verdict(result.verdict) == "TRUE"


def test_contradicting_only_is_contradicted():
    claim = _claim()
    contradicting = [_evidence(claim.claim_id, "https://a.example/1", "contradicts it")]
    result = heuristic_verdict(claim, _matrix(claim, contradicting=contradicting), rounds=1)
    assert result.verdict == "CONTRADICTED"
    assert to_public_verdict(result.verdict) == "FALSE"


def test_supporting_and_contradicting_is_disputed():
    claim = _claim()
    supporting = [_evidence(claim.claim_id, "https://a.example/1", "supports it")]
    contradicting = [_evidence(claim.claim_id, "https://b.example/1", "contradicts it")]
    result = heuristic_verdict(claim, _matrix(claim, supporting=supporting, contradicting=contradicting), rounds=1)
    assert result.verdict == "DISPUTED"


def test_stale_time_sensitive_claim_with_only_old_supporting_evidence_is_outdated():
    claim = local_decompose_claims("The mayor announced a new policy today.")[0]
    assert claim.time_sensitive is True
    supporting = [_evidence(claim.claim_id, "https://a.example/1", "supports it")]
    matrix = _matrix(claim, supporting=supporting, unresolved=[GAP_STALE_TIME_SENSITIVE])
    result = heuristic_verdict(claim, matrix, rounds=2)
    assert result.verdict == "OUTDATED"


def test_heuristic_never_sets_a_confidence_score():
    """No invented confidence number for the deterministic path -- same
    'counts not scores' honesty as Phase 5's source-quality signals."""
    claim = _claim()
    result = heuristic_verdict(claim, _matrix(claim), rounds=1)
    assert result.confidence_score is None
    assert result.confidence_type is None


def test_rationale_explicitly_references_unresolved_gaps():
    """Handoff doc section 7, task 11: must explain uncertainty, not just
    assign a bare label."""
    claim = _claim()
    matrix = _matrix(
        claim,
        unresolved=["no contradicting evidence found despite a contradiction-oriented search", "evidence lacks independent corroboration (single source domain)"],
        supporting=[_evidence(claim.claim_id, "https://a.example/1", "supports it")],
    )
    result = heuristic_verdict(claim, matrix, rounds=1)
    assert "no contradicting evidence found despite a contradiction-oriented search" in result.rationale
    assert "evidence lacks independent corroboration (single source domain)" in result.rationale


def test_rationale_mentions_retrieval_round_count():
    claim = _claim()
    supporting = [_evidence(claim.claim_id, "https://a.example/1", "supports it")]
    result = heuristic_verdict(claim, _matrix(claim, supporting=supporting), rounds=2)
    assert "2 retrieval round" in result.rationale


# --- generate_verdict: selection + fallback orchestration ------------------


def test_generate_verdict_uses_heuristic_when_verifier_not_configured():
    claim = _claim()
    matrix = _matrix(claim, supporting=[_evidence(claim.claim_id, "https://a.example/1", "supports it")])
    verifier = StubVerificationModel(configured=False)

    result = generate_verdict(claim, matrix, 1, verifier)

    assert verifier.assess_matrix_calls == 0  # never even attempted
    assert result.metadata["verifier_used"] == "heuristic_fallback"


def test_generate_verdict_uses_verifier_result_when_configured_and_successful():
    claim = _claim()
    matrix = _matrix(claim)
    expected = heuristic_verdict(claim, matrix, 1)  # any VerificationResult will do as a sentinel
    verifier = StubVerificationModel(configured=True, result=expected)

    result = generate_verdict(claim, matrix, 1, verifier)

    assert verifier.assess_matrix_calls == 1
    assert result is expected


def test_generate_verdict_falls_back_to_heuristic_when_verifier_raises():
    claim = _claim()
    matrix = _matrix(claim, supporting=[_evidence(claim.claim_id, "https://a.example/1", "supports it")])
    verifier = StubVerificationModel(configured=True, raises=RuntimeError("boom"))

    result = generate_verdict(claim, matrix, 1, verifier)

    assert verifier.assess_matrix_calls == 1
    assert result.metadata["verifier_used"] == "heuristic_fallback"
    assert result.verdict == "SUPPORTED"  # heuristic still ran correctly


# --- GeminiEvidenceVerifier.assess_matrix (unconfigured path only --------
# no established precedent in this repo for mocking the google-genai SDK
# response shape; compare()/extract_claim() aren't mocked-tested either) --


def test_gemini_assess_matrix_raises_when_not_configured(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    verifier = GeminiEvidenceVerifier(SETTINGS)
    with pytest.raises(RuntimeError):
        verifier.assess_matrix(_claim(), _matrix(_claim()), rounds=1)


# --- NemotronVerifier.assess_matrix (fully mockable via httpx) ------------


def _settings_with_openrouter(**overrides):
    return replace(SETTINGS, verifier_model="some/openrouter-model", **overrides)


def test_nemotron_assess_matrix_raises_when_not_configured(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    verifier = NemotronVerifier(replace(SETTINGS, verifier_model=None))
    with pytest.raises(RuntimeError):
        verifier.assess_matrix(_claim(), _matrix(_claim()), rounds=1)


def test_nemotron_assess_matrix_returns_parsed_result_when_configured(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    verifier = NemotronVerifier(_settings_with_openrouter())
    claim = _claim()

    def fake_post(url, **kwargs):
        return _FakeResponse(_chat_payload('{"verdict": "SUPPORTED", "rationale": "Backed by one source.", "confidence_score": 0.6}'))

    monkeypatch.setattr(httpx, "post", fake_post)
    result = verifier.assess_matrix(claim, _matrix(claim), rounds=1)

    assert result.verdict == "SUPPORTED"
    assert result.rationale == "Backed by one source."
    assert result.confidence_score == 0.6
    assert result.confidence_type == "heuristic_evidence_confidence"
    assert result.metadata["verifier_used"] == "some/openrouter-model"


def test_nemotron_assess_matrix_propagates_failure_does_not_self_fallback(monkeypatch):
    """Matches compare()'s existing precedent: assess_matrix() itself does
    not catch OpenRouterError -- generate_verdict() is what falls back."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    verifier = NemotronVerifier(_settings_with_openrouter())
    claim = _claim()

    def fake_post(url, **kwargs):
        raise httpx.ConnectError("no network in tests", request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)
    with pytest.raises(Exception):
        verifier.assess_matrix(claim, _matrix(claim), rounds=1)


def test_generate_verdict_falls_back_when_nemotron_configured_but_fails(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    verifier = NemotronVerifier(_settings_with_openrouter())
    claim = _claim()
    matrix = _matrix(claim, supporting=[_evidence(claim.claim_id, "https://a.example/1", "supports it")])

    def fake_post(url, **kwargs):
        raise httpx.ConnectError("no network in tests", request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)
    result = generate_verdict(claim, matrix, 1, verifier)

    assert result.metadata["verifier_used"] == "heuristic_fallback"
    assert result.verdict == "SUPPORTED"


# --- end-to-end wiring via MultiClaimVerificationService / endpoint -------


def test_service_default_construction_includes_a_verifier(tmp_path):
    service = MultiClaimVerificationService(SETTINGS)
    assert service.verifier is not None
    assert service.verifier.configured is False  # no keys configured anywhere in this test env


def test_verify_endpoint_includes_well_formed_verdict():
    claim = _claim()
    from app.services.truth.evidence_providers import EvidenceProvider
    from app.services.truth.domain import ProviderResult

    class EmptyProvider(EvidenceProvider):
        name = "empty"

        def search_sync(self, query, context):
            return ProviderResult(success=True, provider=self.name, evidence=[], latency_ms=0)

    class StubDecomposer:
        def decompose(self, text):
            del text
            return [claim]

    service = MultiClaimVerificationService(SETTINGS, claim_decomposer=StubDecomposer(), evidence_provider=EmptyProvider())

    with TestClient(create_app(verify_service=service)) as client:
        response = client.post("/api/v2/verify", json={"text": claim.original_text})

    assert response.status_code == 200
    verdict = response.json()["claims"][0]["verdict"]
    assert verdict["verdict"] in ("TRUE", "FALSE", "MISLEADING", "INSUFFICIENT_EVIDENCE")
    assert verdict["internal_verdict"] == "INSUFFICIENT_EVIDENCE"  # no evidence at all was retrieved
    assert verdict["verifier"] == "heuristic_fallback"
    assert isinstance(verdict["rationale"], str) and verdict["rationale"]
