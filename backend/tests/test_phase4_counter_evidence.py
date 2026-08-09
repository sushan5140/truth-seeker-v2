from dataclasses import replace
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from app.config import SETTINGS
from app.main import create_app
from app.services.truth.claim_decomposition import local_decompose_claims
from app.services.truth.counter_evidence import generate_counter_evidence_queries
from app.services.truth.domain import Evidence, ProviderError, ProviderResult
from app.services.truth.evidence_processing import hash_evidence_content
from app.services.truth.evidence_providers import EvidenceProvider
from app.services.truth.verify_v2 import MultiClaimVerificationService


# Credential-safety fixture now lives in tests/conftest.py (session-wide autouse).

RETRIEVED_AT = datetime(2026, 8, 9, tzinfo=timezone.utc)


def _factual_claim(text="The city has twelve public parks."):
    return local_decompose_claims(text)[0]


def _opinion_claim():
    return local_decompose_claims("I think summer is the best season.")[0]


def _evidence(claim_id, url, text, *, provider="stub_evidence", metadata=None):
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
        provider=provider,
        content_hash=hash_evidence_content(text),
        metadata=metadata or {},
    )


class StubEvidenceProvider(EvidenceProvider):
    name = "stub_evidence"

    def __init__(self, responses: dict[str, ProviderResult] | None = None):
        self.responses = responses or {}
        self.calls: list[str] = []

    def search_sync(self, query, context):
        self.calls.append(query)
        if query in self.responses:
            return self.responses[query]
        return ProviderResult(success=True, provider=self.name, evidence=[], latency_ms=0)


class StubClaimDecomposer:
    def __init__(self, claims):
        self.claims = claims

    def decompose(self, text):
        del text
        return self.claims


def _all_matrix_evidence(plan):
    """Phase 5 replaced the flat plan.evidence list with a claim-evidence
    matrix (supporting/contradicting/context); tests that only care about
    total retrieved+deduplicated evidence, not stance, use this."""
    return plan.matrix.supporting + plan.matrix.contradicting + plan.matrix.context


# --- generate_counter_evidence_queries -----------------------------------


def test_opinion_and_unverifiable_claims_get_no_query_plan():
    assert generate_counter_evidence_queries(_opinion_claim(), SETTINGS) == []
    unverifiable = local_decompose_claims("abc.")[0]
    assert generate_counter_evidence_queries(unverifiable, SETTINGS) == []


def test_factual_claim_gets_all_five_categories_within_default_cap():
    claim = _factual_claim()
    queries = generate_counter_evidence_queries(claim, SETTINGS)
    categories = {item.category for item in queries}
    assert categories == {"supporting", "contradicting", "official_source", "correction_retraction", "newer_information"}
    assert queries[0].category == "supporting"
    assert queries[0].query == claim.normalized_claim


def test_max_queries_per_claim_truncates_lowest_priority_first():
    claim = _factual_claim()
    settings = replace(SETTINGS, max_queries_per_claim=2)
    queries = generate_counter_evidence_queries(claim, settings)
    assert len(queries) == 2
    assert queries[0].category == "supporting"
    assert queries[1].category == "contradicting"


def test_official_source_query_uses_first_entity_when_available():
    claim = local_decompose_claims("Marie Curie won the Nobel Prize in Stockholm.")[0]
    assert claim.entities  # sanity: heuristic actually found an entity
    queries = generate_counter_evidence_queries(claim, SETTINGS)
    official = next(item for item in queries if item.category == "official_source")
    assert official.query == f"{claim.entities[0]} official statement"


def test_official_source_query_falls_back_to_claim_text_without_entities():
    claim = _factual_claim("It rained.")
    queries = generate_counter_evidence_queries(claim, SETTINGS)
    official = next(item for item in queries if item.category == "official_source")
    assert official.query == f"{claim.normalized_claim} official statement"


def test_newer_information_query_uses_time_reference_when_available():
    claim = local_decompose_claims("The bridge was completed in 2004.")[0]
    assert claim.time_reference == "2004"
    queries = generate_counter_evidence_queries(claim, SETTINGS)
    newer = next(item for item in queries if item.category == "newer_information")
    assert "2004" in newer.query


# --- MultiClaimVerificationService orchestration --------------------------


def test_verify_single_factual_claim_gathers_evidence_with_provenance():
    claim = _factual_claim()
    provider = StubEvidenceProvider(
        responses={
            claim.normalized_claim: ProviderResult(
                success=True,
                provider="stub_evidence",
                evidence=[_evidence(claim.claim_id, "https://example.org/parks", "The city has twelve public parks.")],
                latency_ms=0,
            )
        }
    )
    service = MultiClaimVerificationService(SETTINGS, claim_decomposer=StubClaimDecomposer([claim]), evidence_provider=provider)

    response = service.verify(claim.original_text)

    assert response.multiple_claims_detected is False
    assert len(response.claims) == 1
    plan = response.claims[0]
    # evidence text == claim text, so the heuristic classifier scores it as
    # a clear lexical match with no negation cues -> supporting.
    assert len(plan.matrix.supporting) == 1
    assert plan.matrix.supporting[0].query == claim.normalized_claim
    assert plan.matrix.supporting[0].query_category == "supporting"
    assert plan.provider == "stub_evidence"


def test_verify_multi_claim_input_produces_one_plan_per_claim():
    claims = local_decompose_claims("The city has twelve public parks. I think summer is the best season.")
    provider = StubEvidenceProvider()
    service = MultiClaimVerificationService(SETTINGS, claim_decomposer=StubClaimDecomposer(claims), evidence_provider=provider)

    response = service.verify("irrelevant, decomposer is stubbed")

    assert response.multiple_claims_detected is True
    assert len(response.claims) == 2
    assert response.claims[0].claim_type == "factual"
    assert response.claims[1].claim_type == "opinion"


def test_opinion_claim_never_triggers_a_search():
    provider = StubEvidenceProvider()
    service = MultiClaimVerificationService(SETTINGS, claim_decomposer=StubClaimDecomposer([_opinion_claim()]), evidence_provider=provider)

    response = service.verify("I think summer is the best season.")

    plan = response.claims[0]
    assert plan.queries == []
    assert _all_matrix_evidence(plan) == []
    assert plan.matrix.unresolved == ["no evidence retrieved for this claim"]
    assert provider.calls == []


def test_provider_failure_is_recorded_and_not_fatal():
    claim = _factual_claim()
    settings = replace(SETTINGS, max_queries_per_claim=2)  # supporting + one contradicting query
    queries = generate_counter_evidence_queries(claim, settings)
    failing_query = queries[0].query
    working_query = queries[1].query

    provider = StubEvidenceProvider(
        responses={
            failing_query: ProviderResult(
                success=False,
                provider="stub_evidence",
                errors=[ProviderError(code="SEARCH_PROVIDER_UNAVAILABLE", message="offline", retryable=True)],
                latency_ms=0,
            ),
            working_query: ProviderResult(
                success=True,
                provider="stub_evidence",
                evidence=[_evidence(claim.claim_id, "https://example.org/parks", "The city has twelve public parks, officials confirmed.")],
                latency_ms=0,
            ),
        }
    )
    service = MultiClaimVerificationService(settings, claim_decomposer=StubClaimDecomposer([claim]), evidence_provider=provider)

    plan = service.verify(claim.original_text).claims[0]

    assert plan.provider_errors == ["supporting (round 1): offline"]
    assert len(_all_matrix_evidence(plan)) == 1


def test_duplicate_evidence_across_categories_is_collapsed_but_distinct_evidence_survives():
    """Mirrors the doc's ask: duplicated supporting sources shouldn't drown
    out genuinely distinct evidence a counter-evidence-oriented query finds."""
    claim = _factual_claim()
    settings = replace(SETTINGS, max_queries_per_claim=3)  # supporting + contradicting + official_source queries
    queries = generate_counter_evidence_queries(claim, settings)
    supporting_query, contradicting_query, official_query = (item.query for item in queries)

    duplicate_evidence = _evidence(claim.claim_id, "https://syndicate.example/story", "Identical syndicated coverage.")
    distinct_evidence = _evidence(claim.claim_id, "https://independent.example/story", "A genuinely different report.")

    provider = StubEvidenceProvider(
        responses={
            supporting_query: ProviderResult(success=True, provider="stub_evidence", evidence=[duplicate_evidence], latency_ms=0),
            contradicting_query: ProviderResult(success=True, provider="stub_evidence", evidence=[duplicate_evidence], latency_ms=0),
            official_query: ProviderResult(success=True, provider="stub_evidence", evidence=[distinct_evidence], latency_ms=0),
        }
    )
    service = MultiClaimVerificationService(settings, claim_decomposer=StubClaimDecomposer([claim]), evidence_provider=provider)

    plan = service.verify(claim.original_text).claims[0]
    all_evidence = _all_matrix_evidence(plan)

    urls = sorted(item.url for item in all_evidence)
    assert urls == ["https://independent.example/story", "https://syndicate.example/story"]
    # first occurrence wins provenance, per EvidenceDeduplicator's existing behavior
    duplicate_out = next(item for item in all_evidence if item.url == "https://syndicate.example/story")
    assert duplicate_out.query_category == "supporting"


def test_query_cap_limits_number_of_searches_actually_issued():
    """max_queries_per_claim caps round 1's plan specifically. Phase 6 may
    still add a bounded round 2 on top (covered in test_phase6_iterative_loop.py),
    so this asserts against the round-1 plan size, not raw total provider
    calls across both rounds."""
    claim = _factual_claim()
    settings = replace(SETTINGS, max_queries_per_claim=2)
    provider = StubEvidenceProvider()
    service = MultiClaimVerificationService(settings, claim_decomposer=StubClaimDecomposer([claim]), evidence_provider=provider)

    plan = service.verify(claim.original_text).claims[0]

    assert len(plan.queries) == 2


def test_default_construction_is_fully_offline():
    """No OPENROUTER_API_KEY/SEARXNG_URL configured anywhere in this test
    environment, so default construction must resolve to the mock search
    provider and the deterministic decomposer -- no network required."""
    service = MultiClaimVerificationService(SETTINGS)
    response = service.verify("The city has twelve public parks.")
    assert len(response.claims) == 1
    assert service.evidence_provider.name == "development_mock"


# --- /api/v2/verify endpoint -----------------------------------------------


def test_verify_endpoint_returns_multi_claim_plan():
    claims = local_decompose_claims("The city has twelve public parks. I think summer is the best season.")
    provider = StubEvidenceProvider()
    service = MultiClaimVerificationService(SETTINGS, claim_decomposer=StubClaimDecomposer(claims), evidence_provider=provider)

    with TestClient(create_app(verify_service=service)) as client:
        response = client.post("/api/v2/verify", json={"text": "irrelevant, decomposer is stubbed"})

    assert response.status_code == 200
    body = response.json()
    assert body["multiple_claims_detected"] is True
    assert len(body["claims"]) == 2
    assert body["claims"][0]["claim_type"] == "factual"
    # Phase 4 predates verdict generation (Phase 7); this test only cares
    # about multi-claim retrieval, so just sanity-check verdict exists now.
    assert "verdict" in body["claims"][0]


def test_verify_endpoint_rejects_empty_text():
    with TestClient(create_app(verify_service=MultiClaimVerificationService(SETTINGS))) as client:
        response = client.post("/api/v2/verify", json={"text": ""})
    assert response.status_code == 422
