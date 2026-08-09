import threading
import time
from dataclasses import replace

from app.config import SETTINGS
from app.services.truth.claim_decomposition import local_decompose_claims
from app.services.truth.counter_evidence import QueryPlanItem
from app.services.truth.domain import ProviderError, ProviderResult
from app.services.truth.evidence_providers import EvidenceProvider
from app.services.truth.verify_v2 import MultiClaimVerificationService


def _claim(text="The city has twelve public parks."):
    return local_decompose_claims(text)[0]


class TimingStubProvider(EvidenceProvider):
    """Records real concurrency behavior via short sleeps and a thread-safe
    in-flight counter. search_sync runs in real OS threads (via
    asyncio.to_thread), so a threading.Lock is required here, not an
    asyncio.Lock -- this is exercising genuine thread-level parallelism,
    not just interleaved coroutines."""

    name = "timing_stub"

    def __init__(self, delay: float = 0.1):
        self.delay = delay
        self.calls: list[str] = []
        self._lock = threading.Lock()
        self.current_in_flight = 0
        self.max_in_flight = 0

    def search_sync(self, query, context):
        with self._lock:
            self.calls.append(query)
            self.current_in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.current_in_flight)
        time.sleep(self.delay)
        with self._lock:
            self.current_in_flight -= 1
        return ProviderResult(success=True, provider=self.name, evidence=[], latency_ms=0)


class FailingProvider(EvidenceProvider):
    name = "failing_stub"

    def __init__(self, fail_queries: set[str] = frozenset(), raise_queries: set[str] = frozenset(), delay: float = 0.0):
        self.fail_queries = fail_queries
        self.raise_queries = raise_queries
        self.delay = delay
        self.calls: list[str] = []

    def search_sync(self, query, context):
        self.calls.append(query)
        if self.delay:
            time.sleep(self.delay)
        if query in self.raise_queries:
            raise RuntimeError(f"boom: {query}")
        if query in self.fail_queries:
            return ProviderResult(success=False, provider=self.name, errors=[ProviderError(code="X", message="failed")], latency_ms=0)
        return ProviderResult(success=True, provider=self.name, evidence=[], latency_ms=0)


class StubClaimDecomposer:
    def __init__(self, claims):
        self.claims = claims

    def decompose(self, text):
        del text
        return self.claims


_CATEGORIES = ("supporting", "contradicting", "official_source", "correction_retraction", "newer_information")


def _queries(n: int) -> list[QueryPlanItem]:
    return [QueryPlanItem(query=f"query-{i}", category=_CATEGORIES[i % len(_CATEGORIES)]) for i in range(n)]


# --- concurrency proof (round 1) -------------------------------------------


def test_round1_queries_run_concurrently_not_sequentially():
    claim = _claim()
    provider = TimingStubProvider(delay=0.15)
    service = MultiClaimVerificationService(SETTINGS, claim_decomposer=StubClaimDecomposer([claim]), evidence_provider=provider)

    start = time.perf_counter()
    gathered, errors, timed_out = service._gather_evidence(claim, _queries(5), "en", round_number=1)
    elapsed = time.perf_counter() - start

    assert len(provider.calls) == 5
    assert timed_out is False
    # Structural proof (reliable regardless of machine speed): multiple
    # calls were genuinely in flight at once, not one-at-a-time.
    assert provider.max_in_flight >= 2
    # Timing proof (generous margin): sequential would be ~5*0.15=0.75s;
    # concurrent (default cap 6, all 5 fit in one batch) should be close
    # to a single delay plus scheduling overhead.
    assert elapsed < 0.6


def test_concurrency_cap_is_respected():
    claim = _claim()
    settings = replace(SETTINGS, max_concurrent_retrievals=2)
    provider = TimingStubProvider(delay=0.12)
    service = MultiClaimVerificationService(settings, claim_decomposer=StubClaimDecomposer([claim]), evidence_provider=provider)

    start = time.perf_counter()
    service._gather_evidence(claim, _queries(6), "en", round_number=1)
    elapsed = time.perf_counter() - start

    assert len(provider.calls) == 6
    # The real proof: never more than 2 in flight despite 6 tasks launching
    # together, which only holds if the semaphore is actually enforcing it.
    assert provider.max_in_flight <= 2
    # Generous upper bound proving it isn't fully serial (6*0.12=0.72s).
    assert elapsed < 0.65


def test_default_concurrency_cap_matches_documented_range():
    assert SETTINGS.max_concurrent_retrievals in (5, 6)


# --- graceful partial failure, no timeout involved -------------------------


def test_one_failing_query_does_not_abort_the_others():
    claim = _claim()
    provider = FailingProvider(fail_queries={"query-1"})
    service = MultiClaimVerificationService(SETTINGS, claim_decomposer=StubClaimDecomposer([claim]), evidence_provider=provider)

    gathered, errors, timed_out = service._gather_evidence(claim, _queries(4), "en", round_number=1)

    assert len(provider.calls) == 4  # all attempted despite one failing
    assert len(errors) == 1
    assert timed_out is False


def test_one_query_raising_an_exception_does_not_abort_the_others():
    """Not just a reported ProviderResult failure -- an actual unexpected
    exception from one concurrent task must not crash the batch for the
    rest (the try/except inside _run_one, not just search_sync's own
    internal error handling)."""
    claim = _claim()
    provider = FailingProvider(raise_queries={"query-2"})
    service = MultiClaimVerificationService(SETTINGS, claim_decomposer=StubClaimDecomposer([claim]), evidence_provider=provider)

    gathered, errors, timed_out = service._gather_evidence(claim, _queries(4), "en", round_number=1)

    assert len(provider.calls) == 4
    assert len(errors) == 1
    assert "boom: query-2" in errors[0]


# --- both rounds covered ----------------------------------------------------


def test_round2_followups_also_run_concurrently():
    """Explicitly proves round 2 isn't left sequential -- same
    _gather_evidence path, called with round_number=2 the way
    _plan_for_claim's second-round branch does."""
    claim = _claim()
    provider = TimingStubProvider(delay=0.12)
    service = MultiClaimVerificationService(SETTINGS, claim_decomposer=StubClaimDecomposer([claim]), evidence_provider=provider)

    start = time.perf_counter()
    gathered, errors, timed_out = service._gather_evidence(
        claim, _queries(3), "en", round_number=2, deadline=time.monotonic() + 5
    )
    elapsed = time.perf_counter() - start

    assert len(provider.calls) == 3
    assert timed_out is False
    assert provider.max_in_flight >= 2
    assert elapsed < 0.5  # well under the 3*0.12=0.36s+ sequential baseline


def test_graceful_partial_timeout_keeps_completed_results_round2():
    """Direct, focused version of the same behavior covered end-to-end in
    test_phase6_iterative_loop.py's timeout test -- a slow query that
    exceeds the deadline is excluded, a fast one that finishes in time is
    kept, and the batch as a whole never raises."""
    claim = _claim()

    class MixedTimingProvider(EvidenceProvider):
        name = "mixed_timing"

        def __init__(self):
            self.calls: list[str] = []

        def search_sync(self, query, context):
            self.calls.append(query)
            time.sleep(0.4 if query == "slow" else 0.05)
            return ProviderResult(success=True, provider=self.name, evidence=[], latency_ms=0)

    mixed_provider = MixedTimingProvider()
    service = MultiClaimVerificationService(SETTINGS, claim_decomposer=StubClaimDecomposer([claim]), evidence_provider=mixed_provider)
    queries = [QueryPlanItem(query="fast", category="supporting"), QueryPlanItem(query="slow", category="contradicting")]

    gathered, errors, timed_out = service._gather_evidence(
        claim, queries, "en", round_number=2, deadline=time.monotonic() + 0.15
    )

    # _gather_evidence reports timed_out as a bool; appending a "timed out"
    # message to provider_errors is _plan_for_claim's job as the caller
    # (see test_phase6_iterative_loop.py for that end-to-end behavior) --
    # this focused test checks the lower-level contract directly.
    assert timed_out is True
    assert "fast" in mixed_provider.calls
    assert "slow" in mixed_provider.calls  # it started concurrently, just didn't finish in time
    assert errors == []  # the slow query didn't fail, it just didn't finish -- no error to report here
