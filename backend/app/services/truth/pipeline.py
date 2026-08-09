import threading
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from app.config import SETTINGS, TruthSettings
from app.models import ClaimRequest, ClaimResponse, EvidenceItem, ProviderInfo, TruthStatusResponse
from app.services.truth.cache import ClaimCache
from app.services.truth.claim_decomposition import ClaimDecomposer
from app.services.truth.evidence_processing import EvidenceDeduplicator, make_claim_id
from app.services.truth.evidence_providers import EvidenceProvider, EvidenceSearchContext, create_evidence_provider
from app.services.truth.extractors import WebExtractor, create_extractor
from app.services.truth.nemotron_verifier import NemotronVerifier
from app.services.truth.providers import SearchProvider, create_search_provider
from app.services.truth.ranking import RankedSource, rank_evidence
from app.services.truth.verification_models import ProcessedClaim, VerificationModel
from app.services.truth.verifier import GeminiEvidenceVerifier, normalize_claim


def create_verifier(settings: TruthSettings) -> VerificationModel:
    """Gemini remains the default verifier; Nemotron/OpenRouter only
    activates once both OPENROUTER_API_KEY and TRUTHSEEKER_VERIFIER_MODEL
    are configured, per the Phase 2 handoff instructions."""
    nemotron = NemotronVerifier(settings)
    if nemotron.configured:
        return nemotron
    return GeminiEvidenceVerifier(settings)


class ClaimVerificationService:
    def __init__(
        self,
        settings: TruthSettings = SETTINGS,
        *,
        search_provider: SearchProvider | None = None,
        extractor: WebExtractor | None = None,
        verifier: VerificationModel | None = None,
        evidence_provider: EvidenceProvider | None = None,
        claim_decomposer: ClaimDecomposer | None = None,
        cache: ClaimCache | None = None,
    ):
        self.settings = settings
        self.search_provider = search_provider or create_search_provider(settings)
        self.extractor = extractor or create_extractor(settings, self.search_provider.name)
        self.verifier = verifier or create_verifier(settings)
        self.evidence_provider = evidence_provider or create_evidence_provider(settings, self.search_provider, self.extractor)
        # Constructed and available on the service for Phase 4+ wiring; not
        # yet called from check()/_run_pipeline() -- see Phase 3 report for
        # why /api/check-claim's single-claim response contract is left as-is.
        self.claim_decomposer = claim_decomposer or ClaimDecomposer(settings)
        self.deduplicator = EvidenceDeduplicator()
        self.cache = cache or ClaimCache(settings.cache_path)
        self._inflight: dict[str, threading.Event] = {}
        self._lock = threading.Lock()

    def status(self) -> TruthStatusResponse:
        return TruthStatusResponse(
            backend_status="running",
            # TODO(phase3): field name is stale now that the verifier is
            # provider-neutral -- reports the active verifier's configured
            # state, not specifically Gemini's. Left as-is in Phase 2 to
            # avoid a response-schema change; rename deliberately later.
            gemini_configured=self.verifier.configured,
            search_provider_configured=self.search_provider.name,
            extraction_provider_configured=self.extractor.name,
            cache_available=self.cache.available(),
        )

    def _provider_info(self, verifier: str) -> ProviderInfo:
        return ProviderInfo(search=self.search_provider.name, extractor=self.extractor.name, verifier=verifier)

    def _early_response(self, processed: ProcessedClaim) -> ClaimResponse:
        verdict = "opinion" if processed.kind == "opinion" else "invalid"
        if processed.kind == "question":
            summary = "Questions are not checked directly. Select the factual statement you want to verify."
        elif processed.kind == "opinion":
            summary = "This appears to be an opinion, so no web evidence search was performed."
        else:
            summary = "The selected text does not contain a clear checkable factual claim."
        limitations = ["No source search was performed because the selection was not a factual claim."]
        if processed.multiple_claims_detected:
            limitations.append("The selection contains multiple claims; only the main claim was examined.")
        return ClaimResponse(
            verdict=verdict,
            claim=processed.main_claim,
            confidence="high" if verdict == "opinion" else "low",
            summary=summary,
            limitations=limitations,
            evidence=[],
            provider=self._provider_info("not_used"),
            cached=False,
            checked_at=datetime.now(timezone.utc),
            multiple_claims_detected=processed.multiple_claims_detected,
        )

    def _insufficient(
        self,
        processed: ProcessedClaim,
        limitations: list[str],
        ranked: list[RankedSource] | None = None,
        verifier: str = "not_used",
    ) -> ClaimResponse:
        if not any("does not prove" in item.lower() for item in limitations):
            limitations = [*limitations, "Absence of evidence does not prove the claim is false."]
        evidence = self._evidence_items(ranked or [], ["neutral"] * len(ranked or []))
        return ClaimResponse(
            verdict="insufficient_evidence",
            claim=processed.main_claim,
            confidence="low",
            summary="The searched sources did not provide enough reliable evidence for a supported or contradicted verdict.",
            limitations=limitations,
            evidence=evidence,
            provider=self._provider_info(verifier),
            cached=False,
            checked_at=datetime.now(timezone.utc),
            multiple_claims_detected=processed.multiple_claims_detected,
        )

    @staticmethod
    def _evidence_items(ranked: list[RankedSource], stances: list[str]) -> list[EvidenceItem]:
        items: list[EvidenceItem] = []
        for source, stance in zip(ranked, stances, strict=True):
            evidence = source.evidence
            publisher = evidence.publisher or urlsplit(evidence.url).hostname or "Unknown publisher"
            items.append(
                EvidenceItem(
                    title=evidence.title or evidence.url,
                    url=evidence.url,
                    publisher=publisher,
                    published_at=evidence.published_at,
                    retrieved_at=evidence.retrieved_at,
                    stance=stance,
                    excerpt=" … ".join(source.passages),
                    source_category=source.category,
                    ranking_score=source.score,
                )
            )
        return items

    def _freshness(self, processed: ProcessedClaim) -> timedelta:
        if processed.time_sensitive:
            return timedelta(minutes=self.settings.time_sensitive_cache_minutes)
        return timedelta(hours=self.settings.ordinary_cache_hours)

    def _run_pipeline(self, request: ClaimRequest, processed: ProcessedClaim, key: str) -> ClaimResponse:
        limitations = [f"Only the first {self.settings.max_search_results} search results were examined."]
        if processed.multiple_claims_detected:
            limitations.append("The selection contains multiple claims; only the main claim was checked.")

        provider_result = self.evidence_provider.search_sync(
            processed.main_claim,
            EvidenceSearchContext(
                claim_id=make_claim_id(processed.main_claim),
                language=request.language,
                limit=self.settings.max_search_results,
            ),
        )
        if not provider_result.success:
            result = self._insufficient(processed, limitations + ["The configured search provider was unavailable.", "Absence of evidence does not prove the claim is false."])
            self.cache.set(key, result, self._freshness(processed))
            return result

        deduplicated = self.deduplicator.deduplicate(provider_result.evidence)
        ranked = rank_evidence(processed.main_claim, deduplicated.evidence, self.settings, time_sensitive=processed.time_sensitive)
        if not ranked:
            result = self._insufficient(
                processed,
                limitations + ["No relevant extractable passage was found.", "Absence of evidence does not prove the claim is false."],
            )
            self.cache.set(key, result, self._freshness(processed))
            return result

        try:
            assessment = self.verifier.compare(processed.main_claim, ranked)
            evidence = self._evidence_items(ranked, assessment.source_stances)
            result = ClaimResponse(
                verdict=assessment.verdict,
                claim=processed.main_claim,
                confidence=assessment.confidence,
                summary=assessment.summary,
                limitations=limitations + ["The verdict is limited to the retrieved passages and sources shown."],
                evidence=evidence,
                # TODO(phase3): hardcoded "gemini" regardless of which VerificationModel
                # actually ran (see create_verifier() above) -- harmless while Nemotron
                # stays unconfigured everywhere, but misreports the verifier once it's
                # ever turned on. Should read from self.verifier instead; deferred so
                # this Phase 2 commit doesn't change ClaimResponse.provider.verifier values.
                provider=self._provider_info("gemini"),
                cached=False,
                checked_at=datetime.now(timezone.utc),
                multiple_claims_detected=processed.multiple_claims_detected,
            )
        except Exception:
            result = self._insufficient(
                processed,
                limitations + ["Gemini verification was unavailable or invalid; no evidence verdict was inferred locally."],
                ranked,
                "local_fallback",
            )
        self.cache.set(key, result, self._freshness(processed))
        return result

    def check(self, request: ClaimRequest) -> ClaimResponse:
        processed = self.verifier.extract_claim(request.text)
        if processed.kind != "factual":
            return self._early_response(processed)

        key = normalize_claim(processed.main_claim)
        if not request.bypass_cache:
            cached = self.cache.get(key)
            if cached:
                return cached

        with self._lock:
            existing = self._inflight.get(key)
            if existing is None:
                event = threading.Event()
                self._inflight[key] = event
                leader = True
            else:
                event = existing
                leader = False

        if not leader:
            event.wait(self.settings.duplicate_wait_seconds)
            cached = self.cache.get(key)
            if cached:
                return cached
            return self._insufficient(processed, ["A duplicate request did not complete before the timeout."])

        try:
            return self._run_pipeline(request, processed, key)
        finally:
            with self._lock:
                self._inflight.pop(key, None)
                event.set()
