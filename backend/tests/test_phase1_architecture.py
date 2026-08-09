import asyncio
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.config import SETTINGS
from app.services.truth.domain import (
    AtomicClaim,
    ClaimEvidenceMatrix,
    Evidence,
    ProviderError,
    ProviderResult,
    VerificationResult,
)
from app.services.truth.evidence_processing import (
    EvidenceDeduplicator,
    EvidenceNormalizer,
    canonicalize_url,
    hash_evidence_content,
)
from app.services.truth.evidence_providers import (
    EvidenceProvider,
    EvidenceSearchContext,
    LegacyRetrievalEvidenceProvider,
)
from app.services.truth.extractors import ExtractedPage, WebExtractor
from app.services.truth.providers import SearchProvider, SearchProviderError, SearchResult
from app.services.truth.verification_models import VerificationModel
from app.services.truth.verifier import GeminiEvidenceVerifier


RETRIEVED_AT = datetime(2026, 8, 8, tzinfo=timezone.utc)


class ProviderStubSearch(SearchProvider):
    name = "phase1_stub"

    def __init__(self, error: Exception | None = None):
        self.error = error

    def search(self, claim, language, limit):
        del claim, language, limit
        if self.error:
            raise self.error
        return [SearchResult("Report", "https://example.org/report", "The report contains twelve entries.")]


class ProviderStubExtractor(WebExtractor):
    name = "phase1_extractor"

    def extract(self, url):
        return ExtractedPage(
            title="Report",
            url=url,
            publisher="Example Institute",
            published_at="2026-08-01",
            retrieved_at=RETRIEVED_AT,
            passages=["The report contains twelve entries in the current inventory."],
        )


def normalized_evidence(*, url="https://example.org/report", text="The report contains twelve entries.") -> Evidence:
    canonical_url = canonicalize_url(url)
    return Evidence(
        evidence_id=f"evidence_{hash_evidence_content(url + text)[:24]}",
        claim_id="claim_001",
        url=url,
        canonical_url=canonical_url,
        publisher="Example Institute",
        author=None,
        title="Report",
        published_at="2026-08-01",
        retrieved_at=RETRIEVED_AT,
        source_type="webpage",
        primary_or_secondary=None,
        text=text,
        evidence_chunk=text,
        provider="phase1_stub",
        content_hash=hash_evidence_content(text),
        metadata={},
    )


def test_canonical_domain_schemas_validate_and_link_evidence():
    claim = AtomicClaim(
        claim_id="claim_001",
        original_text="The report contains twelve entries.",
        normalized_claim="the report contains twelve entries",
        claim_type="factual",
        entities=["report"],
        location=None,
        time_reference="current",
        time_sensitive=True,
        verification_requirements=["current inventory"],
    )
    evidence = normalized_evidence()
    matrix = ClaimEvidenceMatrix(claim=claim, supporting=[evidence], unresolved=[])
    result = VerificationResult(
        claim=claim,
        verdict="SUPPORTED",
        rationale="The retained evidence directly supports the atomic claim.",
        evidence_ids=[evidence.evidence_id],
        matrix=matrix,
        confidence_score=0.8,
        confidence_type="heuristic_evidence_confidence",
        verified_at=RETRIEVED_AT,
    )

    assert result.matrix.supporting[0].claim_id == claim.claim_id
    assert result.verdict == "SUPPORTED"


def test_evidence_rejects_unsafe_url_and_incorrect_content_hash():
    evidence = normalized_evidence()
    with pytest.raises(ValidationError):
        Evidence.model_validate({**evidence.model_dump(), "url": "http://127.0.0.1/private"})

    with pytest.raises(ValidationError, match="content_hash"):
        Evidence.model_validate({**evidence.model_dump(), "content_hash": "0" * 64})


def test_normalization_preserves_provenance_and_unknown_primary_status():
    original_url = "https://Example.org/report?utm_source=test&b=2&a=1#details"
    result = SearchResult("Search title", original_url, "Search result snippet.", "2026-08-01", None)
    page = ExtractedPage(
        title="Extracted title",
        url=original_url,
        publisher="example.org",
        published_at=None,
        retrieved_at=RETRIEVED_AT,
        passages=["First extracted passage with useful evidence.", "Second extracted passage."],
    )

    evidence = EvidenceNormalizer().normalize(
        "claim_001",
        result,
        page,
        search_provider="searxng",
        extraction_provider="lightweight_http",
    )

    assert evidence.url == original_url
    assert evidence.canonical_url == "https://example.org/report?a=1&b=2"
    assert evidence.retrieved_at == RETRIEVED_AT
    assert evidence.provider == "searxng"
    assert evidence.title == "Extracted title"
    assert evidence.evidence_chunk == "Search result snippet."
    assert evidence.publisher is None
    assert evidence.primary_or_secondary is None
    assert evidence.metadata["extraction_provider"] == "lightweight_http"
    assert evidence.metadata["passages"] == page.passages


def test_canonical_url_and_content_hash_are_deterministic():
    first = canonicalize_url("HTTPS://Example.org/path?utm_medium=x&z=2&a=1#section")
    second = canonicalize_url("https://example.org/path?a=1&z=2")

    assert first == second == "https://example.org/path?a=1&z=2"
    assert hash_evidence_content("Evidence   with\nspacing") == hash_evidence_content("Evidence with spacing")


def test_exact_duplicate_detection_and_group_metadata():
    first = normalized_evidence()
    same_content = normalized_evidence(url="https://mirror.example.net/report")
    same_url = normalized_evidence(url="https://example.org/report", text="A corrected version of the report.")

    result = EvidenceDeduplicator().deduplicate([first, same_content, same_url])

    assert len(result.evidence) == 1
    assert result.duplicate_ids[first.evidence_id] == [same_content.evidence_id, same_url.evidence_id]
    assert result.evidence[0].metadata["domain_group"] == "example.org"
    assert result.evidence[0].metadata["publisher_group"] == "example institute"
    assert result.evidence[0].metadata["exact_duplicate_count"] == 2


def test_provider_failure_is_structured():
    provider = LegacyRetrievalEvidenceProvider(ProviderStubSearch(SearchProviderError("offline")), ProviderStubExtractor())
    result = provider.search_sync("claim", EvidenceSearchContext(claim_id="claim_001"))

    assert result.success is False
    assert result.evidence == []
    assert result.errors[0].code == "SEARCH_PROVIDER_UNAVAILABLE"
    assert result.errors[0].retryable is True
    assert result.latency_ms >= 0


def test_provider_timeout_is_structured():
    provider = LegacyRetrievalEvidenceProvider(ProviderStubSearch(TimeoutError("slow")), ProviderStubExtractor())
    result = provider.search_sync("claim", EvidenceSearchContext(claim_id="claim_001"))

    assert result.success is False
    assert result.errors == [ProviderError(code="PROVIDER_TIMEOUT", message="The search provider timed out.", retryable=True)]


def test_provider_is_async_compatible_without_live_network_calls():
    provider = LegacyRetrievalEvidenceProvider(ProviderStubSearch(), ProviderStubExtractor())
    result = asyncio.run(provider.search("claim", EvidenceSearchContext(claim_id="claim_001")))

    assert isinstance(provider, EvidenceProvider)
    assert result.success is True
    assert len(result.evidence) == 1
    assert result.evidence[0].metadata["extraction_provider"] == "phase1_extractor"


def test_provider_result_requires_error_when_failed():
    with pytest.raises(ValidationError):
        ProviderResult(success=False, provider="stub", latency_ms=0)


def test_gemini_implements_provider_neutral_verification_model():
    verifier = GeminiEvidenceVerifier(SETTINGS)

    assert isinstance(verifier, VerificationModel)
    assert type(verifier).compare is not VerificationModel.compare
    assert type(verifier).extract_claim is not VerificationModel.extract_claim
