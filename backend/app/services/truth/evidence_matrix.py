from dataclasses import dataclass

from app.services.truth.domain import AtomicClaim, ClaimEvidenceMatrix, Evidence
from app.services.truth.evidence_classification import EvidenceClassifier


@dataclass(frozen=True)
class SourceQualityCounts:
    """Neutral, factual signals -- not a trust or confidence score. Source
    quality should inform reasoning, not automatically determine truth
    (handoff doc section 17); this deliberately stops at counting, not
    scoring or ranking sources by presumed trustworthiness."""

    total_evidence: int
    independent_domain_count: int
    independent_publisher_count: int
    evidence_with_published_date_count: int
    duplicate_evidence_removed_count: int


@dataclass(frozen=True)
class ClaimAuditResult:
    matrix: ClaimEvidenceMatrix
    source_quality: SourceQualityCounts
    classifier_name: str


def _source_quality(evidence_items: list[Evidence]) -> SourceQualityCounts:
    domains = {item.metadata.get("domain_group") for item in evidence_items if item.metadata.get("domain_group")}
    publishers = {item.metadata.get("publisher_group") for item in evidence_items if item.metadata.get("publisher_group")}
    duplicates_removed = sum(int(item.metadata.get("exact_duplicate_count") or 0) for item in evidence_items)
    with_date = sum(1 for item in evidence_items if item.published_at)
    return SourceQualityCounts(
        total_evidence=len(evidence_items),
        independent_domain_count=len(domains),
        independent_publisher_count=len(publishers),
        evidence_with_published_date_count=with_date,
        duplicate_evidence_removed_count=duplicates_removed,
    )


def _analyze_gaps(
    claim: AtomicClaim,
    evidence_items: list[Evidence],
    supporting: list[Evidence],
    contradicting: list[Evidence],
) -> list[str]:
    """Deterministic evidence-gap checks. Populates ClaimEvidenceMatrix.unresolved
    (gap descriptions, not evidence items) -- this is the auditor's honest
    account of what's missing, separate from the supporting/contradicting/
    context grouping above."""
    if not evidence_items:
        return ["no evidence retrieved for this claim"]

    gaps: list[str] = []

    if not supporting and not contradicting:
        gaps.append("no evidence clearly supports or contradicts this claim; all retrieved evidence was classified as context")
    if not contradicting:
        gaps.append("no contradicting evidence found despite a contradiction-oriented search")
    if not any(item.metadata.get("query_category") == "official_source" for item in evidence_items):
        gaps.append("no evidence from an official-source-oriented search was retrieved")

    domains = {item.metadata.get("domain_group") for item in evidence_items if item.metadata.get("domain_group")}
    if len(domains) <= 1:
        gaps.append("evidence lacks independent corroboration (single source domain)")

    if claim.time_sensitive and not any(item.metadata.get("query_category") == "newer_information" for item in evidence_items):
        gaps.append("no up-to-date information found for this time-sensitive claim")

    if not any(item.published_at for item in evidence_items):
        gaps.append("no retrieved evidence includes a publication date")

    return gaps


def build_claim_audit(claim: AtomicClaim, evidence_items: list[Evidence], classifier: EvidenceClassifier) -> ClaimAuditResult:
    classification = classifier.classify(claim, evidence_items)

    supporting: list[Evidence] = []
    contradicting: list[Evidence] = []
    context: list[Evidence] = []
    for item in evidence_items:
        stance = classification.stances.get(item.evidence_id, "context")
        if stance == "supporting":
            supporting.append(item)
        elif stance == "contradicting":
            contradicting.append(item)
        else:
            context.append(item)

    unresolved = _analyze_gaps(claim, evidence_items, supporting, contradicting)

    matrix = ClaimEvidenceMatrix(
        claim=claim,
        supporting=supporting,
        contradicting=contradicting,
        context=context,
        unresolved=unresolved,
    )

    return ClaimAuditResult(
        matrix=matrix,
        source_quality=_source_quality(evidence_items),
        classifier_name=classification.classifier_name,
    )
