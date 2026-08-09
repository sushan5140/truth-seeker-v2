from pydantic import BaseModel, ConfigDict, Field

from app.config import SETTINGS, TruthSettings
from app.services.truth.claim_decomposition import ClaimDecomposer
from app.services.truth.counter_evidence import QueryPlanItem, generate_counter_evidence_queries
from app.services.truth.domain import AtomicClaim, Evidence
from app.services.truth.evidence_processing import EvidenceDeduplicator
from app.services.truth.evidence_providers import EvidenceProvider, EvidenceSearchContext, create_evidence_provider
from app.services.truth.extractors import WebExtractor, create_extractor
from app.services.truth.providers import SearchProvider, create_search_provider


class VerifyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=5000)
    language: str = Field(default="en", min_length=2, max_length=16)


class EvidenceWithProvenance(BaseModel):
    title: str | None
    url: str
    publisher: str | None
    published_at: str | None
    query: str
    query_category: str
    excerpt: str


class ClaimVerificationPlan(BaseModel):
    """Evidence-gathering result for one atomic claim.

    Deliberately has no verdict field -- Phase 4 is counter-evidence
    retrieval only. Reasoning over this evidence into a verdict is
    Phase 5+ scope (claim-evidence matrix / verification engine).
    """

    claim_id: str
    original_text: str
    normalized_claim: str
    claim_type: str
    time_sensitive: bool
    entities: list[str]
    location: str | None
    time_reference: str | None
    queries: list[QueryPlanItem]
    evidence: list[EvidenceWithProvenance]
    provider: str
    provider_errors: list[str]


class VerifyResponse(BaseModel):
    claims: list[ClaimVerificationPlan]
    multiple_claims_detected: bool


def _to_evidence_out(evidence: Evidence) -> EvidenceWithProvenance:
    return EvidenceWithProvenance(
        title=evidence.title,
        url=evidence.url,
        publisher=evidence.publisher,
        published_at=evidence.published_at,
        query=str(evidence.metadata.get("query", "")),
        query_category=str(evidence.metadata.get("query_category", "")),
        excerpt=evidence.evidence_chunk,
    )


class MultiClaimVerificationService:
    """Orchestrates Phase 3 decomposition + Phase 4 counter-evidence
    retrieval for /api/v2/verify. Reuses the existing Phase 1 provider
    stack (SearchProvider/WebExtractor/EvidenceProvider/EvidenceDeduplicator)
    unchanged -- SSRF protection, timeouts, and dedup all apply exactly as
    they do for /api/check-claim, since it's the same underlying code."""

    def __init__(
        self,
        settings: TruthSettings = SETTINGS,
        *,
        claim_decomposer: ClaimDecomposer | None = None,
        search_provider: SearchProvider | None = None,
        extractor: WebExtractor | None = None,
        evidence_provider: EvidenceProvider | None = None,
    ):
        self.settings = settings
        self.claim_decomposer = claim_decomposer or ClaimDecomposer(settings)
        resolved_search_provider = search_provider or create_search_provider(settings)
        resolved_extractor = extractor or create_extractor(settings, resolved_search_provider.name)
        self.evidence_provider = evidence_provider or create_evidence_provider(settings, resolved_search_provider, resolved_extractor)
        self.deduplicator = EvidenceDeduplicator()

    def verify(self, text: str, language: str = "en") -> VerifyResponse:
        claims = self.claim_decomposer.decompose(text)
        plans = [self._plan_for_claim(claim, language) for claim in claims]
        return VerifyResponse(claims=plans, multiple_claims_detected=len(claims) > 1)

    def _plan_for_claim(self, claim: AtomicClaim, language: str) -> ClaimVerificationPlan:
        queries = generate_counter_evidence_queries(claim, self.settings)

        gathered: list[Evidence] = []
        errors: list[str] = []
        for item in queries:
            context = EvidenceSearchContext(
                claim_id=claim.claim_id,
                language=language,
                limit=self.settings.max_search_results,
            )
            result = self.evidence_provider.search_sync(item.query, context)
            if not result.success:
                errors.extend(f"{item.category}: {error.message}" for error in result.errors)
                continue
            gathered.extend(
                evidence.model_copy(
                    update={"metadata": {**evidence.metadata, "query": item.query, "query_category": item.category}}
                )
                for evidence in result.evidence
            )

        deduplicated = self.deduplicator.deduplicate(gathered)

        return ClaimVerificationPlan(
            claim_id=claim.claim_id,
            original_text=claim.original_text,
            normalized_claim=claim.normalized_claim,
            claim_type=claim.claim_type,
            time_sensitive=claim.time_sensitive,
            entities=claim.entities,
            location=claim.location,
            time_reference=claim.time_reference,
            queries=queries,
            evidence=[_to_evidence_out(evidence) for evidence in deduplicated.evidence],
            provider=self.evidence_provider.name,
            provider_errors=errors,
        )
