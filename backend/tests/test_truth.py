import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.config import SETTINGS
from app.models import ClaimRequest, GeminiEvidenceAssessment
from app.services.truth.cache import ClaimCache
from app.services.truth.extractors import ExtractionError, ExtractedPage, WebExtractor, _clean_html
from app.services.truth.pipeline import ClaimVerificationService
from app.services.truth.providers import SearchProvider, SearchProviderError, SearchResult
from app.services.truth.security import UnsafeUrlError, validate_remote_url
from app.services.truth.verifier import local_claim_processing


FIXTURES = Path(__file__).parent / "fixtures"


class StubSearch(SearchProvider):
    name = "test_search"

    def __init__(self, results=None, *, error=False, delay=0):
        self.results = results or [SearchResult("Official parks report", "https://example.org/parks", "The city has twelve public parks.")]
        self.error = error
        self.delay = delay
        self.calls = 0

    def search(self, claim, language, limit):
        del claim, language
        self.calls += 1
        if self.delay:
            time.sleep(self.delay)
        if self.error:
            raise SearchProviderError("offline")
        return self.results[:limit]


class StubExtractor(WebExtractor):
    name = "test_extractor"

    def __init__(self, passages=None, *, error=False):
        self.passages = passages or ["The city has twelve public parks according to the official inventory released this year."]
        self.error = error
        self.calls = 0

    def extract(self, url):
        self.calls += 1
        if self.error:
            raise ExtractionError("blocked")
        return ExtractedPage(
            title=f"Evidence {self.calls}",
            url=url,
            publisher="Example Institute",
            published_at="2026-06-01",
            retrieved_at=datetime.now(timezone.utc),
            passages=[f"{passage} Source item {self.calls}." for passage in self.passages],
        )


class StubVerifier:
    def __init__(self, verdict="supported", *, error=False, configured=True):
        self.verdict = verdict
        self.error = error
        self.api_key = "configured" if configured else ""
        self.compare_calls = 0
        self.captured_evidence = []

    @property
    def configured(self):
        return bool(self.api_key)

    def extract_claim(self, text):
        return local_claim_processing(text)

    def compare(self, claim, evidence):
        del claim
        self.compare_calls += 1
        self.captured_evidence = evidence
        if self.error:
            raise TimeoutError("mock Gemini timeout")
        stances = {
            "supported": "supports",
            "contradicted": "contradicts",
            "mixed": "mixed",
            "insufficient_evidence": "neutral",
        }
        return GeminiEvidenceAssessment(
            verdict=self.verdict,
            confidence="medium" if self.verdict == "mixed" else "high",
            summary=f"Mock {self.verdict} assessment based only on supplied evidence.",
            source_stances=[stances.get(self.verdict, "neutral")] * len(evidence),
        )


def build_service(tmp_path, *, search=None, extractor=None, verifier=None, **setting_overrides):
    settings = replace(SETTINGS, cache_path=tmp_path / "cache.sqlite3", **setting_overrides)
    return ClaimVerificationService(
        settings,
        search_provider=search or StubSearch(),
        extractor=extractor or StubExtractor(),
        verifier=verifier or StubVerifier(),
        cache=ClaimCache(settings.cache_path),
    )


@pytest.mark.parametrize("verdict", ["supported", "contradicted", "mixed", "insufficient_evidence"])
def test_structured_verdicts(tmp_path, verdict):
    service = build_service(tmp_path, verifier=StubVerifier(verdict))
    result = service.check(ClaimRequest(text="The city has twelve public parks.", bypass_cache=True))
    assert result.verdict == verdict
    assert result.evidence
    assert result.provider.verifier == "gemini"
    assert "not absolute certainty" in result.certainty_notice


def test_factual_claim_detection():
    assert local_claim_processing("The city has twelve public parks.").kind == "factual"


def test_opinion_and_question_skip_search(tmp_path):
    search = StubSearch()
    service = build_service(tmp_path, search=search)
    assert service.check(ClaimRequest(text="I think summer is the best season.")).verdict == "opinion"
    assert service.check(ClaimRequest(text="Has the city opened twelve public parks?")).verdict == "invalid"
    assert search.calls == 0


@pytest.mark.parametrize("text", ["nonsense", "123 456 789", "blue trees quickly"])
def test_invalid_input_is_not_searched(tmp_path, text):
    search = StubSearch()
    result = build_service(tmp_path, search=search).check(ClaimRequest(text=text))
    assert result.verdict == "invalid"
    assert search.calls == 0


def test_search_provider_failure_is_honest(tmp_path):
    result = build_service(tmp_path, search=StubSearch(error=True)).check(ClaimRequest(text="The city has twelve public parks.", bypass_cache=True))
    assert result.verdict == "insufficient_evidence"
    assert not result.evidence
    assert any("search provider" in item.lower() for item in result.limitations)


def test_extraction_failure_is_honest(tmp_path):
    result = build_service(tmp_path, extractor=StubExtractor(error=True)).check(ClaimRequest(text="The city has twelve public parks.", bypass_cache=True))
    assert result.verdict == "insufficient_evidence"
    assert not result.evidence


@pytest.mark.parametrize("url", ["javascript:alert(1)", "data:text/plain,test", "file:///tmp/a", "http://localhost/a", "http://127.0.0.1/a", "http://10.0.0.4/a", "http://[::1]/a"])
def test_unsafe_url_rejection(url):
    with pytest.raises((UnsafeUrlError, ValueError)):
        validate_remote_url(url, resolve_dns=False)


def test_private_dns_resolution_is_rejected(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_args, **_kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.20", 443))])
    with pytest.raises(UnsafeUrlError):
        validate_remote_url("https://public-looking.example/page", resolve_dns=True)


def test_pipeline_drops_unsafe_search_result(tmp_path):
    search = StubSearch([SearchResult("Unsafe", "http://127.0.0.1/admin", "The city has twelve public parks.")])
    result = build_service(tmp_path, search=search).check(ClaimRequest(text="The city has twelve public parks.", bypass_cache=True))
    assert result.verdict == "insufficient_evidence"
    assert result.evidence == []


def test_malformed_html_is_cleaned_without_crashing():
    page = _clean_html("<html><title>Broken<body><main><p>The city has twelve public parks in the official inventory", "https://example.org/broken")
    assert page.title == "Broken"
    assert page.passages


def test_prompt_injection_remains_untrusted_evidence(tmp_path):
    html = (FIXTURES / "prompt_injection.html").read_text(encoding="utf-8")
    page = _clean_html(html, "https://example.org/injection")

    class FixtureExtractor(StubExtractor):
        def extract(self, url):
            return page

    verifier = StubVerifier("supported")
    result = build_service(tmp_path, extractor=FixtureExtractor(), verifier=verifier).check(ClaimRequest(text="The city has twelve public parks.", bypass_cache=True))
    assert result.verdict == "supported"
    assert verifier.compare_calls == 1
    assert all("<script" not in passage.lower() and "<form" not in passage.lower() for source in verifier.captured_evidence for passage in source.passages)


def test_missing_gemini_key_never_fabricates_verdict(tmp_path):
    result = build_service(tmp_path, verifier=StubVerifier(configured=False, error=True)).check(ClaimRequest(text="The city has twelve public parks.", bypass_cache=True))
    assert result.verdict == "insufficient_evidence"
    assert result.provider.verifier == "local_fallback"


@pytest.mark.parametrize("error_type", [TimeoutError, ValueError])
def test_gemini_timeout_or_malformed_response_uses_insufficient_evidence(tmp_path, error_type):
    class BrokenVerifier(StubVerifier):
        def compare(self, claim, evidence):
            del claim, evidence
            raise error_type("mock failure")

    result = build_service(tmp_path, verifier=BrokenVerifier()).check(ClaimRequest(text="The city has twelve public parks.", bypass_cache=True))
    assert result.verdict == "insufficient_evidence"
    assert result.provider.verifier == "local_fallback"


def test_cache_hit_and_bypass(tmp_path):
    search = StubSearch()
    service = build_service(tmp_path, search=search)
    first = service.check(ClaimRequest(text="The city has twelve public parks."))
    second = service.check(ClaimRequest(text="The city has twelve public parks."))
    third = service.check(ClaimRequest(text="The city has twelve public parks.", bypass_cache=True))
    assert first.cached is False
    assert second.cached is True
    assert third.cached is False
    assert search.calls == 2


def test_cache_expiry(tmp_path):
    service = build_service(tmp_path)
    result = service.check(ClaimRequest(text="The city has twelve public parks.", bypass_cache=True))
    key = "the city has twelve public parks"
    service.cache.set(key, result, timedelta(seconds=-1))
    assert service.cache.get(key, datetime.now(timezone.utc)) is None


def test_duplicate_request_suppression(tmp_path):
    search = StubSearch(delay=0.15)
    verifier = StubVerifier()
    service = build_service(tmp_path, search=search, verifier=verifier)
    request = ClaimRequest(text="The city has twelve public parks.", bypass_cache=True)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(service.check, [request, request]))
    assert search.calls == 1
    assert verifier.compare_calls == 1
    assert {result.verdict for result in results} == {"supported"}


def test_source_and_passage_limits(tmp_path):
    results = [SearchResult(f"Source {index}", f"https://example.org/source-{index}", "The city has twelve public parks.") for index in range(5)]
    passages = ["The city has twelve public parks and this deliberately long passage contains additional evidence words " * 4 for _ in range(4)]
    verifier = StubVerifier()
    service = build_service(
        tmp_path,
        search=StubSearch(results),
        extractor=StubExtractor(passages),
        verifier=verifier,
        max_search_results=5,
        max_evidence_sources=3,
        max_passages_per_source=2,
        max_passage_length=120,
    )
    service.check(ClaimRequest(text="The city has twelve public parks.", bypass_cache=True))
    assert len(verifier.captured_evidence) <= 3
    assert all(len(source.passages) <= 2 for source in verifier.captured_evidence)
    assert all(len(passage) <= 120 for source in verifier.captured_evidence for passage in source.passages)


def test_extension_claim_routing_and_safe_rendering_assumptions():
    project = Path(__file__).resolve().parents[2]
    background = (project / "extension" / "background.js").read_text(encoding="utf-8")
    content = (project / "extension" / "claim-content.js").read_text(encoding="utf-8")
    popup = (project / "extension" / "popup" / "popup.js").read_text(encoding="utf-8")
    assert 'message?.type === "CHECK_CLAIM"' in background
    assert 'message?.type === "CANCEL_CLAIM"' in background
    assert 'message?.type === "OPEN_SOURCE"' in background
    assert 'type: "CHECK_CLAIM"' in content
    assert "textContent" in content and "innerHTML" not in content
    assert "textContent" in popup and "innerHTML" not in popup
    assert "javascript:" not in content
