import time

from pydantic import BaseModel, ConfigDict, Field

from app.config import SETTINGS, TruthSettings
from app.services.truth.citation_validator import validate_rationale
from app.services.truth.claim_decomposition import ClaimDecomposer
from app.services.truth.counter_evidence import QueryPlanItem, generate_counter_evidence_queries, generate_followup_queries
from app.services.truth.domain import AtomicClaim, Evidence
from app.services.truth.evidence_classification import EvidenceClassifier, score_relevance
from app.services.truth.evidence_matrix import build_claim_audit
from app.services.truth.evidence_processing import EvidenceDeduplicator
from app.services.truth.evidence_providers import EvidenceProvider, EvidenceSearchContext, create_evidence_provider
from app.services.truth.extractors import WebExtractor, create_extractor
from app.services.truth.pipeline import create_verifier
from app.services.truth.providers import SearchProvider, create_search_provider
from app.services.truth.verdict_generation import PublicVerdict, generate_verdict, to_public_verdict
from app.services.truth.verification_models import VerificationModel


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
    retrieval_round: int
    excerpt: str
    relevance_score: float


class SourceQualitySignals(BaseModel):
    total_evidence: int
    independent_domain_count: int
    independent_publisher_count: int
    evidence_with_published_date_count: int
    duplicate_evidence_removed_count: int


class ClaimEvidenceMatrixOut(BaseModel):
    supporting: list[EvidenceWithProvenance]
    contradicting: list[EvidenceWithProvenance]
    context: list[EvidenceWithProvenance]
    unresolved: list[str]


class VerdictOut(BaseModel):
    """verdict is the simple public vocabulary (handoff doc section 10.5's
    backward-compat mapping); internal_verdict is the richer domain.py
    InternalVerdict this collapsed from, kept for transparency/debugging.
    rationale has already passed through Phase 8's citation validator --
    sentences that don't trace to retrieved evidence, or that cite a URL
    not actually in the retrieved evidence, are silently stripped before
    this response is built."""

    verdict: PublicVerdict
    internal_verdict: str
    rationale: str
    confidence_score: float | None
    verifier: str


class ClaimVerificationPlan(BaseModel):
    """Evidence-gathering, evidence-auditing, and verdict result for one
    atomic claim."""

    claim_id: str
    original_text: str
    normalized_claim: str
    claim_type: str
    time_sensitive: bool
    entities: list[str]
    location: str | None
    time_reference: str | None
    queries: list[QueryPlanItem]
    followup_queries: list[QueryPlanItem]
    rounds: int
    matrix: ClaimEvidenceMatrixOut
    source_quality: SourceQualitySignals
    classifier: str
    verdict: VerdictOut
    provider: str
    provider_errors: list[str]


class VerifyResponse(BaseModel):
    claims: list[ClaimVerificationPlan]
    multiple_claims_detected: bool


def _to_evidence_out(claim: AtomicClaim, evidence: Evidence) -> EvidenceWithProvenance:
    return EvidenceWithProvenance(
        title=evidence.title,
        url=evidence.url,
        publisher=evidence.publisher,
        published_at=evidence.published_at,
        query=str(evidence.metadata.get("query", "")),
        query_category=str(evidence.metadata.get("query_category", "")),
        retrieval_round=int(evidence.metadata.get("retrieval_round", 1)),
        excerpt=evidence.evidence_chunk,
        relevance_score=score_relevance(claim, evidence),
    )


class MultiClaimVerificationService:
    """Orchestrates Phase 3 decomposition, Phase 4 counter-evidence
    retrieval, and Phase 5 evidence auditing for /api/v2/verify. Reuses the
    existing Phase 1 provider stack (SearchProvider/WebExtractor/
    EvidenceProvider/EvidenceDeduplicator) unchanged -- SSRF protection,
    timeouts, and dedup all apply exactly as they do for /api/check-claim,
    since it's the same underlying code."""

    def __init__(
        self,
        settings: TruthSettings = SETTINGS,
        *,
        claim_decomposer: ClaimDecomposer | None = None,
        search_provider: SearchProvider | None = None,
        extractor: WebExtractor | None = None,
        evidence_provider: EvidenceProvider | None = None,
        classifier: EvidenceClassifier | None = None,
        verifier: VerificationModel | None = None,
    ):
        self.settings = settings
        self.claim_decomposer = claim_decomposer or ClaimDecomposer(settings)
        resolved_search_provider = search_provider or create_search_provider(settings)
        resolved_extractor = extractor or create_extractor(settings, resolved_search_provider.name)
        self.evidence_provider = evidence_provider or create_evidence_provider(settings, resolved_search_provider, resolved_extractor)
        self.deduplicator = EvidenceDeduplicator()
        self.classifier = classifier or EvidenceClassifier(settings)
        # Reuses Phase 1/2's exact selection logic: Nemotron when configured,
        # Gemini otherwise -- same gating as ClaimVerificationService, nothing
        # new invented here.
        self.verifier = verifier or create_verifier(settings)

    def verify(self, text: str, language: str = "en") -> VerifyResponse:
        claims = self.claim_decomposer.decompose(text)
        plans = [self._plan_for_claim(claim, language) for claim in claims]
        return VerifyResponse(claims=plans, multiple_claims_detected=len(claims) > 1)

    def _gather_evidence(
        self, claim: AtomicClaim, queries: list[QueryPlanItem], language: str, *, round_number: int, deadline: float | None = None
    ) -> tuple[list[Evidence], list[str], bool]:
        """Runs queries in order, tagging each result with its query/category/
        round for provenance. If deadline (time.monotonic() cutoff) is given
        and is reached before a query starts, remaining queries in this call
        are skipped and timed_out=True is returned -- used for Phase 6's
        second-round time budget; round 1 never passes a deadline."""
        gathered: list[Evidence] = []
        errors: list[str] = []
        timed_out = False
        for item in queries:
            if deadline is not None and time.monotonic() >= deadline:
                timed_out = True
                break
            context = EvidenceSearchContext(
                claim_id=claim.claim_id,
                language=language,
                limit=self.settings.max_search_results,
            )
            result = self.evidence_provider.search_sync(item.query, context)
            if not result.success:
                errors.extend(f"{item.category} (round {round_number}): {error.message}" for error in result.errors)
                continue
            gathered.extend(
                evidence.model_copy(
                    update={
                        "metadata": {
                            **evidence.metadata,
                            "query": item.query,
                            "query_category": item.category,
                            "retrieval_round": round_number,
                        }
                    }
                )
                for evidence in result.evidence
            )
        return gathered, errors, timed_out

    def _plan_for_claim(self, claim: AtomicClaim, language: str) -> ClaimVerificationPlan:
        queries = generate_counter_evidence_queries(claim, self.settings)
        gathered, errors, _ = self._gather_evidence(claim, queries, language, round_number=1)
        deduplicated = self.deduplicator.deduplicate(gathered).evidence
        audit = build_claim_audit(claim, deduplicated, self.classifier)

        # Phase 6: at most one additional, targeted retrieval round -- a hard
        # cap by construction (a single "if", not a loop), never a second
        # attempt at a second attempt.
        followup_queries = generate_followup_queries(claim, audit.matrix.unresolved)
        rounds = 1
        if followup_queries:
            rounds = 2
            deadline = time.monotonic() + self.settings.second_round_timeout_seconds
            round2_evidence, round2_errors, timed_out = self._gather_evidence(
                claim, followup_queries, language, round_number=2, deadline=deadline
            )
            errors.extend(round2_errors)
            if timed_out:
                errors.append("round 2: timed out before all targeted follow-up queries could run")
            if round2_evidence:
                deduplicated = self.deduplicator.deduplicate(gathered + round2_evidence).evidence
                audit = build_claim_audit(claim, deduplicated, self.classifier)
            # If round 2 produced nothing new (failed/timed out/all duplicates),
            # the round-1 audit stands -- unresolved gaps are returned as-is,
            # never as an error.

        matrix_out = ClaimEvidenceMatrixOut(
            supporting=[_to_evidence_out(claim, item) for item in audit.matrix.supporting],
            contradicting=[_to_evidence_out(claim, item) for item in audit.matrix.contradicting],
            context=[_to_evidence_out(claim, item) for item in audit.matrix.context],
            unresolved=audit.matrix.unresolved,
        )

        verification = generate_verdict(claim, audit.matrix, rounds, self.verifier)
        # Phase 8: strip any sentence in the rationale that doesn't trace
        # back to retrieved evidence (or cites a URL that isn't actually
        # retrieved evidence) before it ever reaches the response -- this
        # applies uniformly whether the rationale came from a model or the
        # deterministic heuristic, since either could in principle contain
        # unsupported content.
        validated = validate_rationale(verification.rationale, audit.matrix)
        verdict_out = VerdictOut(
            verdict=to_public_verdict(verification.verdict),
            internal_verdict=verification.verdict,
            rationale=validated.rationale,
            confidence_score=verification.confidence_score,
            verifier=str(verification.metadata.get("verifier_used", "heuristic_fallback")),
        )

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
            followup_queries=followup_queries,
            rounds=rounds,
            matrix=matrix_out,
            source_quality=SourceQualitySignals(
                total_evidence=audit.source_quality.total_evidence,
                independent_domain_count=audit.source_quality.independent_domain_count,
                independent_publisher_count=audit.source_quality.independent_publisher_count,
                evidence_with_published_date_count=audit.source_quality.evidence_with_published_date_count,
                duplicate_evidence_removed_count=audit.source_quality.duplicate_evidence_removed_count,
            ),
            classifier=audit.classifier_name,
            verdict=verdict_out,
            provider=self.evidence_provider.name,
            provider_errors=errors,
        )
