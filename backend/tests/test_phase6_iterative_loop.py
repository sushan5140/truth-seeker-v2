import time
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from app.config import SETTINGS
from app.main import create_app
from app.services.truth.claim_decomposition import local_decompose_claims
from app.services.truth.counter_evidence import generate_counter_evidence_queries, generate_followup_queries
from app.services.truth.domain import Evidence, ProviderResult
from app.services.truth.evidence_matrix import (
    GAP_NO_CLEAR_STANCE,
    GAP_NO_CONTRADICTING,
    GAP_NO_EVIDENCE,
    GAP_NO_OFFICIAL_SOURCE,
    GAP_NO_PUBLICATION_DATE,
    GAP_SINGLE_DOMAIN,
    GAP_STALE_TIME_SENSITIVE,
)
from app.services.truth.evidence_processing import hash_evidence_content
from app.services.truth.evidence_providers import EvidenceProvider
from app.services.truth.verify_v2 import MultiClaimVerificationService


# Credential-safety fixture now lives in tests/conftest.py (session-wide autouse).


RETRIEVED_AT = datetime(2026, 8, 9, tzinfo=timezone.utc)


def _claim(text="The city has twelve public parks."):
    return local_decompose_claims(text)[0]


def _evidence(claim_id, url, text, *, domain_group=None, published_at=None):
    metadata = {}
    if domain_group is not None:
        metadata["domain_group"] = domain_group
    return Evidence(
        evidence_id=f"evidence_{hash_evidence_content(url + text)[:24]}",
        claim_id=claim_id,
        url=url,
        canonical_url=url,
        publisher="Example Institute",
        author=None,
        title="Title",
        published_at=published_at,
        retrieved_at=RETRIEVED_AT,
        source_type="webpage",
        primary_or_secondary=None,
        text=text,
        evidence_chunk=text,
        provider="stub",
        content_hash=hash_evidence_content(text),
        metadata=metadata,
    )


class StubEvidenceProvider(EvidenceProvider):
    name = "stub_evidence"

    def __init__(self, responses: dict[str, ProviderResult] | None = None):
        self.responses = responses or {}
        self.calls: list[str] = []

    def search_sync(self, query, context):
        self.calls.append(query)
        return self.responses.get(query, ProviderResult(success=True, provider=self.name, evidence=[], latency_ms=0))


class StubClaimDecomposer:
    def __init__(self, claims):
        self.claims = claims

    def decompose(self, text):
        del text
        return self.claims


# --- generate_followup_queries -------------------------------------------


def test_no_gaps_produces_no_followups():
    assert generate_followup_queries(_claim(), []) == []


def test_no_evidence_gap_short_circuits_to_single_supporting_retry():
    claim = _claim()
    followups = generate_followup_queries(claim, [GAP_NO_EVIDENCE, GAP_NO_CONTRADICTING])
    assert len(followups) == 1
    assert followups[0].category == "supporting"
    assert followups[0].query == claim.normalized_claim


def test_missing_contradicting_gap_produces_a_different_phrasing_than_round1():
    claim = _claim()
    round1_queries = {item.category: item.query for item in generate_counter_evidence_queries(claim, SETTINGS)}
    followups = generate_followup_queries(claim, [GAP_NO_CONTRADICTING])
    assert len(followups) == 1
    assert followups[0].category == "contradicting"
    assert followups[0].query != round1_queries["contradicting"]
    assert followups[0].query == f"{claim.normalized_claim} debunked"


def test_missing_official_source_gap_produces_a_different_phrasing_than_round1():
    claim = _claim()
    round1_queries = {item.category: item.query for item in generate_counter_evidence_queries(claim, SETTINGS)}
    followups = generate_followup_queries(claim, [GAP_NO_OFFICIAL_SOURCE])
    assert followups[0].category == "official_source"
    assert followups[0].query != round1_queries["official_source"]


def test_stale_time_sensitive_gap_produces_a_different_phrasing_than_round1():
    claim = local_decompose_claims("The mayor announced a new policy today.")[0]
    round1_queries = {item.category: item.query for item in generate_counter_evidence_queries(claim, SETTINGS)}
    followups = generate_followup_queries(claim, [GAP_STALE_TIME_SENSITIVE])
    assert followups[0].category == "newer_information"
    assert followups[0].query != round1_queries["newer_information"]


def test_multiple_actionable_gaps_produce_multiple_followups_in_order():
    claim = _claim()
    followups = generate_followup_queries(claim, [GAP_NO_CONTRADICTING, GAP_NO_OFFICIAL_SOURCE])
    assert [item.category for item in followups] == ["contradicting", "official_source"]


def test_non_actionable_gaps_produce_no_followups():
    claim = _claim()
    followups = generate_followup_queries(claim, [GAP_SINGLE_DOMAIN, GAP_NO_PUBLICATION_DATE, GAP_NO_CLEAR_STANCE])
    assert followups == []


# --- end-to-end iterative loop via MultiClaimVerificationService ---------


def test_second_round_not_triggered_when_round1_leaves_no_gaps():
    claim = _claim()
    queries = generate_counter_evidence_queries(claim, SETTINGS)
    by_category = {item.category: item.query for item in queries}

    supporting_ev = _evidence(claim.claim_id, "https://a.example/1", "The city has twelve public parks, confirmed.", domain_group="a.example", published_at="2026-01-01")
    contradicting_ev = _evidence(claim.claim_id, "https://b.example/1", "Officials denied the city has twelve public parks.", domain_group="b.example")
    official_ev = _evidence(claim.claim_id, "https://c.example/1", "The city has twelve public parks per official records.", domain_group="c.example")

    provider = StubEvidenceProvider(
        responses={
            by_category["supporting"]: ProviderResult(success=True, provider="stub", evidence=[supporting_ev], latency_ms=0),
            by_category["contradicting"]: ProviderResult(success=True, provider="stub", evidence=[contradicting_ev], latency_ms=0),
            by_category["official_source"]: ProviderResult(success=True, provider="stub", evidence=[official_ev], latency_ms=0),
        }
    )
    service = MultiClaimVerificationService(SETTINGS, claim_decomposer=StubClaimDecomposer([claim]), evidence_provider=provider)

    plan = service.verify(claim.original_text).claims[0]

    assert plan.matrix.unresolved == []
    assert plan.rounds == 1
    assert plan.followup_queries == []
    assert len(provider.calls) == len(queries)  # only round 1 ran


def test_second_round_triggered_and_resolves_the_gap():
    claim = _claim()
    queries = generate_counter_evidence_queries(claim, SETTINGS)
    by_category = {item.category: item.query for item in queries}

    supporting_ev = _evidence(claim.claim_id, "https://a.example/1", "The city has twelve public parks, confirmed.", domain_group="a.example", published_at="2026-01-01")
    official_ev = _evidence(claim.claim_id, "https://b.example/1", "The city has twelve public parks per official records.", domain_group="b.example")
    # "contradicting" round-1 query deliberately left unanswered -> gap fires

    followup_query = generate_followup_queries(claim, [GAP_NO_CONTRADICTING])[0].query
    round2_contradicting_ev = _evidence(claim.claim_id, "https://c.example/1", "Officials debunked the claim the city has twelve public parks.", domain_group="c.example")

    provider = StubEvidenceProvider(
        responses={
            by_category["supporting"]: ProviderResult(success=True, provider="stub", evidence=[supporting_ev], latency_ms=0),
            by_category["official_source"]: ProviderResult(success=True, provider="stub", evidence=[official_ev], latency_ms=0),
            followup_query: ProviderResult(success=True, provider="stub", evidence=[round2_contradicting_ev], latency_ms=0),
        }
    )
    service = MultiClaimVerificationService(SETTINGS, claim_decomposer=StubClaimDecomposer([claim]), evidence_provider=provider)

    plan = service.verify(claim.original_text).claims[0]

    assert plan.rounds == 2
    assert [item.query for item in plan.followup_queries] == [followup_query]
    assert len(plan.matrix.contradicting) == 1
    assert plan.matrix.contradicting[0].query == followup_query
    assert plan.matrix.contradicting[0].retrieval_round == 2
    assert GAP_NO_CONTRADICTING not in plan.matrix.unresolved
    assert followup_query in provider.calls


def test_second_round_graceful_fallback_when_gap_remains_unresolved():
    """The follow-up query itself finds nothing new -- must return normally
    with the gap still listed, never raise or block."""
    claim = _claim()
    queries = generate_counter_evidence_queries(claim, SETTINGS)
    by_category = {item.category: item.query for item in queries}

    supporting_ev = _evidence(claim.claim_id, "https://a.example/1", "The city has twelve public parks, confirmed.", domain_group="a.example", published_at="2026-01-01")
    official_ev = _evidence(claim.claim_id, "https://b.example/1", "The city has twelve public parks per official records.", domain_group="b.example")
    # follow-up query intentionally left unanswered too -> empty result

    provider = StubEvidenceProvider(
        responses={
            by_category["supporting"]: ProviderResult(success=True, provider="stub", evidence=[supporting_ev], latency_ms=0),
            by_category["official_source"]: ProviderResult(success=True, provider="stub", evidence=[official_ev], latency_ms=0),
        }
    )
    service = MultiClaimVerificationService(SETTINGS, claim_decomposer=StubClaimDecomposer([claim]), evidence_provider=provider)

    plan = service.verify(claim.original_text).claims[0]

    assert plan.rounds == 2  # a second round was genuinely attempted
    assert GAP_NO_CONTRADICTING in plan.matrix.unresolved  # still unresolved, honestly reported
    assert plan.matrix.contradicting == []


def test_second_round_timeout_skips_remaining_followups(monkeypatch):
    claim = _claim()
    queries = generate_counter_evidence_queries(claim, SETTINGS)
    by_category = {item.category: item.query for item in queries}

    # Leave both "contradicting" and "official_source" unanswered in round 1
    # so two follow-up queries are generated for round 2.
    supporting_ev = _evidence(claim.claim_id, "https://a.example/1", "The city has twelve public parks, confirmed.", domain_group="a.example")

    followups = generate_followup_queries(claim, [GAP_NO_CONTRADICTING, GAP_NO_OFFICIAL_SOURCE])
    assert [item.category for item in followups] == ["contradicting", "official_source"]
    first_followup_query, second_followup_query = (item.query for item in followups)
    round2_ev = _evidence(claim.claim_id, "https://c.example/1", "Officials debunked the claim the city has twelve public parks.", domain_group="c.example")

    provider = StubEvidenceProvider(
        responses={
            by_category["supporting"]: ProviderResult(success=True, provider="stub", evidence=[supporting_ev], latency_ms=0),
            first_followup_query: ProviderResult(success=True, provider="stub", evidence=[round2_ev], latency_ms=0),
            second_followup_query: ProviderResult(success=True, provider="stub", evidence=[round2_ev], latency_ms=0),
        }
    )
    service = MultiClaimVerificationService(SETTINGS, claim_decomposer=StubClaimDecomposer([claim]), evidence_provider=provider)

    # 3 time.monotonic() calls expected: deadline calc, round-2 iteration 1
    # check (before deadline), round-2 iteration 2 check (past deadline).
    clock = iter([1000.0, 1000.0, 1020.0])
    monkeypatch.setattr(time, "monotonic", lambda: next(clock))

    plan = service.verify(claim.original_text).claims[0]

    assert plan.rounds == 2
    assert second_followup_query not in provider.calls  # skipped due to timeout
    assert first_followup_query in provider.calls  # first one still ran
    assert any("timed out" in error for error in plan.provider_errors)
    assert GAP_NO_OFFICIAL_SOURCE in plan.matrix.unresolved  # never got its retry


def test_no_third_round_is_ever_attempted():
    """Hard cap: even if round 2's own results still leave the exact same
    gap unresolved, there is no round 3."""
    claim = _claim()
    queries = generate_counter_evidence_queries(claim, SETTINGS)
    by_category = {item.category: item.query for item in queries}
    supporting_ev = _evidence(claim.claim_id, "https://a.example/1", "The city has twelve public parks, confirmed.", domain_group="a.example")

    provider = StubEvidenceProvider(
        responses={by_category["supporting"]: ProviderResult(success=True, provider="stub", evidence=[supporting_ev], latency_ms=0)}
    )
    service = MultiClaimVerificationService(SETTINGS, claim_decomposer=StubClaimDecomposer([claim]), evidence_provider=provider)

    plan = service.verify(claim.original_text).claims[0]
    total_calls_after_first_verify = len(provider.calls)

    # Calling verify again on a fresh service/provider call count is not
    # relevant here; the real assertion is structural: rounds is capped at 2
    # and follow-up query count matches exactly the gap-mapped categories
    # from round 1 (no repeated/escalating retry behavior).
    assert plan.rounds == 2
    assert total_calls_after_first_verify == len(queries) + len(plan.followup_queries)


# --- /api/v2/verify endpoint -----------------------------------------------


def test_verify_endpoint_exposes_rounds_and_followup_queries():
    claim = _claim()
    provider = StubEvidenceProvider()
    service = MultiClaimVerificationService(SETTINGS, claim_decomposer=StubClaimDecomposer([claim]), evidence_provider=provider)

    with TestClient(create_app(verify_service=service)) as client:
        response = client.post("/api/v2/verify", json={"text": claim.original_text})

    assert response.status_code == 200
    plan = response.json()["claims"][0]
    assert "rounds" in plan
    assert "followup_queries" in plan
    assert plan["rounds"] == 2  # nothing answered at all -> every gap fires -> second round attempted
    # Phase 6 predates verdict generation (Phase 7); sanity-check it's there now.
    assert "verdict" in plan
