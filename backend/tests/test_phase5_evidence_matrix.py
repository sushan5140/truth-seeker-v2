from dataclasses import replace
from datetime import datetime, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import SETTINGS
from app.main import create_app
from app.services.truth.claim_decomposition import local_decompose_claims
from app.services.truth.domain import Evidence, ProviderResult
from app.services.truth.evidence_classification import (
    EvidenceClassifier,
    classify_stance_heuristic,
    score_relevance,
)
from app.services.truth.evidence_matrix import build_claim_audit
from app.services.truth.evidence_processing import hash_evidence_content
from app.services.truth.evidence_providers import EvidenceProvider
from app.services.truth.verify_v2 import MultiClaimVerificationService


RETRIEVED_AT = datetime(2026, 8, 9, tzinfo=timezone.utc)


def _claim(text="The city has twelve public parks."):
    return local_decompose_claims(text)[0]


def _evidence(claim_id, url, text, *, domain_group=None, published_at=None, query_category=None, exact_duplicate_count=0):
    metadata = {}
    if domain_group is not None:
        metadata["domain_group"] = domain_group
    if query_category is not None:
        metadata["query_category"] = query_category
    if exact_duplicate_count:
        metadata["exact_duplicate_count"] = exact_duplicate_count
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

    def search_sync(self, query, context):
        return self.responses.get(query, ProviderResult(success=True, provider=self.name, evidence=[], latency_ms=0))


class StubClaimDecomposer:
    def __init__(self, claims):
        self.claims = claims

    def decompose(self, text):
        del text
        return self.claims


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _chat_payload(content: str) -> dict:
    return {"choices": [{"message": {"content": content}}]}


# --- score_relevance / classify_stance_heuristic --------------------------


def test_relevance_score_full_overlap_is_high():
    claim = _claim()
    evidence = _evidence(claim.claim_id, "https://example.org/a", claim.normalized_claim)
    assert score_relevance(claim, evidence) == 100.0


def test_relevance_score_no_overlap_is_zero():
    claim = _claim()
    evidence = _evidence(claim.claim_id, "https://example.org/a", "Completely unrelated sentence about cats.")
    assert score_relevance(claim, evidence) == 0.0


def test_heuristic_classifies_overlapping_evidence_as_supporting():
    claim = _claim()
    evidence = _evidence(claim.claim_id, "https://example.org/a", "The city has twelve public parks, officials confirmed.")
    assert classify_stance_heuristic(claim, evidence) == "supporting"


def test_heuristic_classifies_negated_overlapping_evidence_as_contradicting():
    claim = _claim()
    evidence = _evidence(claim.claim_id, "https://example.org/a", "Officials denied the city has twelve public parks.")
    assert classify_stance_heuristic(claim, evidence) == "contradicting"


def test_heuristic_classifies_low_overlap_evidence_as_context():
    claim = _claim()
    evidence = _evidence(claim.claim_id, "https://example.org/a", "A completely different topic entirely.")
    assert classify_stance_heuristic(claim, evidence) == "context"


def test_heuristic_borderline_overlap_uses_query_category_as_tiebreak_only():
    # claim words: {the, city, has, twelve, public, parks} (6). This text
    # overlaps only on "the"/"city" (2/6 = 0.333), landing inside the
    # borderline band [0.25, 0.40) with no negation cues either way.
    claim = _claim()
    borderline_text = "The city report covered many unrelated topics entirely."
    tagged_contradicting = _evidence(claim.claim_id, "https://example.org/a", borderline_text, query_category="contradicting")
    untagged = _evidence(claim.claim_id, "https://example.org/b", borderline_text, query_category="supporting")

    assert classify_stance_heuristic(claim, tagged_contradicting) == "contradicting"
    assert classify_stance_heuristic(claim, untagged) == "supporting"


# --- EvidenceClassifier (OpenRouter routing + fallback) --------------------


def test_classifier_not_configured_without_api_key(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    classifier = EvidenceClassifier(replace(SETTINGS, classifier_model="some/model"))
    assert classifier.configured is False


def test_classifier_not_configured_without_model(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    classifier = EvidenceClassifier(replace(SETTINGS, classifier_model=None))
    assert classifier.configured is False


def test_classifier_uses_heuristic_when_not_configured(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    claim = _claim()
    evidence = _evidence(claim.claim_id, "https://example.org/a", "The city has twelve public parks, officials confirmed.")
    classifier = EvidenceClassifier(replace(SETTINGS, classifier_model=None))

    result = classifier.classify(claim, [evidence])

    assert result.classifier_name == "heuristic"
    assert result.stances[evidence.evidence_id] == "supporting"


def test_classifier_skips_network_call_for_empty_evidence(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

    def fail_if_called(*args, **kwargs):
        raise AssertionError("should not call the network for an empty evidence list")

    monkeypatch.setattr(httpx, "post", fail_if_called)
    classifier = EvidenceClassifier(replace(SETTINGS, classifier_model="some/model"))

    result = classifier.classify(_claim(), [])
    assert result.stances == {}
    assert result.classifier_name == "heuristic"


def test_classifier_falls_back_on_openrouter_network_failure(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    claim = _claim()
    evidence = _evidence(claim.claim_id, "https://example.org/a", "The city has twelve public parks, officials confirmed.")
    classifier = EvidenceClassifier(replace(SETTINGS, classifier_model="some/model"))

    def fake_post(url, **kwargs):
        raise httpx.ConnectError("no network in tests", request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)
    result = classifier.classify(claim, [evidence])

    assert result.classifier_name == "heuristic"
    assert result.stances[evidence.evidence_id] == "supporting"


def test_classifier_falls_back_on_malformed_response(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    claim = _claim()
    evidence = _evidence(claim.claim_id, "https://example.org/a", "The city has twelve public parks, officials confirmed.")
    classifier = EvidenceClassifier(replace(SETTINGS, classifier_model="some/model"))

    def fake_post(url, **kwargs):
        return _FakeResponse(_chat_payload("not valid json"))

    monkeypatch.setattr(httpx, "post", fake_post)
    result = classifier.classify(claim, [evidence])

    assert result.classifier_name == "heuristic"


def test_classifier_uses_model_output_when_configured(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    claim = _claim()
    evidence = _evidence(claim.claim_id, "https://example.org/a", "The city has twelve public parks, officials confirmed.")
    classifier = EvidenceClassifier(replace(SETTINGS, classifier_model="some/model"))

    content = f'{{"classifications": [{{"evidence_id": "{evidence.evidence_id}", "stance": "context"}}]}}'

    def fake_post(url, **kwargs):
        return _FakeResponse(_chat_payload(content))

    monkeypatch.setattr(httpx, "post", fake_post)
    result = classifier.classify(claim, [evidence])

    assert result.classifier_name == "some/model"
    # model overrides the heuristic's "supporting" verdict for this item
    assert result.stances[evidence.evidence_id] == "context"


def test_classifier_falls_back_per_item_for_evidence_ids_the_model_omitted(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    claim = _claim()
    covered = _evidence(claim.claim_id, "https://example.org/a", "The city has twelve public parks, officials confirmed.")
    omitted = _evidence(claim.claim_id, "https://example.org/b", "Officials denied the city has twelve public parks.")
    classifier = EvidenceClassifier(replace(SETTINGS, classifier_model="some/model"))

    content = f'{{"classifications": [{{"evidence_id": "{covered.evidence_id}", "stance": "context"}}]}}'

    def fake_post(url, **kwargs):
        return _FakeResponse(_chat_payload(content))

    monkeypatch.setattr(httpx, "post", fake_post)
    result = classifier.classify(claim, [covered, omitted])

    assert result.stances[covered.evidence_id] == "context"  # from the model
    assert result.stances[omitted.evidence_id] == "contradicting"  # heuristic fallback, not dropped


# --- build_claim_audit: grouping, gap analysis, source quality ------------


def test_audit_groups_evidence_into_supporting_contradicting_context():
    claim = _claim()
    supporting = _evidence(claim.claim_id, "https://a.example/1", "The city has twelve public parks, confirmed.", domain_group="a.example")
    contradicting = _evidence(claim.claim_id, "https://b.example/1", "Officials denied the city has twelve public parks.", domain_group="b.example")
    context = _evidence(claim.claim_id, "https://c.example/1", "An unrelated municipal budget report.", domain_group="c.example")
    classifier = EvidenceClassifier(replace(SETTINGS, classifier_model=None))

    audit = build_claim_audit(claim, [supporting, contradicting, context], classifier)

    assert [item.url for item in audit.matrix.supporting] == ["https://a.example/1"]
    assert [item.url for item in audit.matrix.contradicting] == ["https://b.example/1"]
    assert [item.url for item in audit.matrix.context] == ["https://c.example/1"]


def test_audit_conflicting_evidence_does_not_report_missing_contradiction():
    """The doc explicitly asks for a conflicting-evidence test: supporting
    and contradicting evidence coexisting should be reflected accurately,
    not papered over as a gap."""
    claim = _claim()
    supporting = _evidence(claim.claim_id, "https://a.example/1", "The city has twelve public parks, confirmed.", domain_group="a.example")
    contradicting = _evidence(claim.claim_id, "https://b.example/1", "Officials denied the city has twelve public parks.", domain_group="b.example")
    classifier = EvidenceClassifier(replace(SETTINGS, classifier_model=None))

    audit = build_claim_audit(claim, [supporting, contradicting], classifier)

    assert audit.matrix.supporting and audit.matrix.contradicting
    assert "no contradicting evidence found despite a contradiction-oriented search" not in audit.matrix.unresolved
    assert "no evidence clearly supports or contradicts this claim; all retrieved evidence was classified as context" not in audit.matrix.unresolved


def test_audit_flags_missing_official_source_evidence():
    """Stands in for the doc's 'missing primary documents' scenario: Phase 5
    doesn't implement real primary/secondary source detection (deliberately
    not guessed, per handoff doc section 10.2/17), so an official-source
    search producing nothing is the closest honest signal available."""
    claim = _claim()
    supporting = _evidence(claim.claim_id, "https://a.example/1", "The city has twelve public parks, confirmed.", domain_group="a.example", query_category="supporting")
    classifier = EvidenceClassifier(replace(SETTINGS, classifier_model=None))

    audit = build_claim_audit(claim, [supporting], classifier)

    assert "no evidence from an official-source-oriented search was retrieved" in audit.matrix.unresolved

    official = _evidence(claim.claim_id, "https://gov.example/1", "The city has twelve public parks per the official registry.", domain_group="gov.example", query_category="official_source")
    audit_with_official = build_claim_audit(claim, [supporting, official], classifier)
    assert "no evidence from an official-source-oriented search was retrieved" not in audit_with_official.matrix.unresolved


def test_audit_flags_single_domain_corroboration():
    claim = _claim()
    only_domain = _evidence(claim.claim_id, "https://a.example/1", "The city has twelve public parks, confirmed.", domain_group="a.example")
    classifier = EvidenceClassifier(replace(SETTINGS, classifier_model=None))

    single = build_claim_audit(claim, [only_domain], classifier)
    assert "evidence lacks independent corroboration (single source domain)" in single.matrix.unresolved

    other_domain = _evidence(claim.claim_id, "https://b.example/1", "The city has twelve public parks, confirmed elsewhere.", domain_group="b.example")
    multi = build_claim_audit(claim, [only_domain, other_domain], classifier)
    assert "evidence lacks independent corroboration (single source domain)" not in multi.matrix.unresolved


def test_audit_flags_time_sensitive_claim_missing_newer_information():
    claim = local_decompose_claims("The mayor announced a new policy today.")[0]
    assert claim.time_sensitive is True
    stale = _evidence(claim.claim_id, "https://a.example/1", "The mayor announced a new policy today, per officials.", domain_group="a.example", query_category="supporting")
    classifier = EvidenceClassifier(replace(SETTINGS, classifier_model=None))

    audit = build_claim_audit(claim, [stale], classifier)
    assert "no up-to-date information found for this time-sensitive claim" in audit.matrix.unresolved

    fresh = _evidence(claim.claim_id, "https://b.example/1", "The mayor announced a new policy today, latest update.", domain_group="b.example", query_category="newer_information")
    audit_with_fresh = build_claim_audit(claim, [stale, fresh], classifier)
    assert "no up-to-date information found for this time-sensitive claim" not in audit_with_fresh.matrix.unresolved


def test_audit_flags_missing_publication_dates():
    claim = _claim()
    undated = _evidence(claim.claim_id, "https://a.example/1", "The city has twelve public parks, confirmed.", domain_group="a.example")
    classifier = EvidenceClassifier(replace(SETTINGS, classifier_model=None))

    audit = build_claim_audit(claim, [undated], classifier)
    assert "no retrieved evidence includes a publication date" in audit.matrix.unresolved

    dated = _evidence(claim.claim_id, "https://b.example/1", "The city has twelve public parks, confirmed.", domain_group="b.example", published_at="2026-08-01")
    audit_with_date = build_claim_audit(claim, [undated, dated], classifier)
    assert "no retrieved evidence includes a publication date" not in audit_with_date.matrix.unresolved


def test_audit_no_evidence_yields_single_gap_and_empty_buckets():
    claim = _claim()
    classifier = EvidenceClassifier(replace(SETTINGS, classifier_model=None))

    audit = build_claim_audit(claim, [], classifier)

    assert audit.matrix.unresolved == ["no evidence retrieved for this claim"]
    assert audit.matrix.supporting == audit.matrix.contradicting == audit.matrix.context == []
    assert audit.source_quality.total_evidence == 0


def test_source_quality_signals_count_correctly():
    claim = _claim()
    items = [
        _evidence(claim.claim_id, "https://a.example/1", "text one", domain_group="a.example", published_at="2026-01-01", exact_duplicate_count=2),
        _evidence(claim.claim_id, "https://b.example/1", "text two", domain_group="b.example"),
    ]
    classifier = EvidenceClassifier(replace(SETTINGS, classifier_model=None))

    audit = build_claim_audit(claim, items, classifier)

    assert audit.source_quality.total_evidence == 2
    assert audit.source_quality.independent_domain_count == 2
    assert audit.source_quality.evidence_with_published_date_count == 1
    assert audit.source_quality.duplicate_evidence_removed_count == 2
    assert audit.classifier_name == "heuristic"


# --- end-to-end: MultiClaimVerificationService / /api/v2/verify -----------


def test_verify_end_to_end_populates_matrix_and_source_quality():
    claim = _claim()
    provider = StubEvidenceProvider(
        responses={
            claim.normalized_claim: ProviderResult(
                success=True,
                provider="stub_evidence",
                evidence=[_evidence(claim.claim_id, "https://a.example/1", "The city has twelve public parks, confirmed.", domain_group="a.example")],
                latency_ms=0,
            ),
            f"{claim.normalized_claim} false": ProviderResult(
                success=True,
                provider="stub_evidence",
                evidence=[_evidence(claim.claim_id, "https://b.example/1", "Officials denied the city has twelve public parks.", domain_group="b.example")],
                latency_ms=0,
            ),
        }
    )
    service = MultiClaimVerificationService(SETTINGS, claim_decomposer=StubClaimDecomposer([claim]), evidence_provider=provider)

    plan = service.verify(claim.original_text).claims[0]

    assert len(plan.matrix.supporting) == 1
    assert len(plan.matrix.contradicting) == 1
    assert plan.source_quality.total_evidence == 2
    assert plan.source_quality.independent_domain_count == 2
    assert plan.classifier == "heuristic"


def test_verify_endpoint_response_shape_has_matrix_no_verdict():
    claim = _claim()
    provider = StubEvidenceProvider()
    service = MultiClaimVerificationService(SETTINGS, claim_decomposer=StubClaimDecomposer([claim]), evidence_provider=provider)

    with TestClient(create_app(verify_service=service)) as client:
        response = client.post("/api/v2/verify", json={"text": claim.original_text})

    assert response.status_code == 200
    plan = response.json()["claims"][0]
    assert set(plan["matrix"].keys()) == {"supporting", "contradicting", "context", "unresolved"}
    assert "source_quality" in plan
    assert "classifier" in plan
    assert "verdict" not in plan
    assert "verdict" not in plan["matrix"]
