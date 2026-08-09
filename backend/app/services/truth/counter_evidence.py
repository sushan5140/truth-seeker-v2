from typing import Callable, Literal

from pydantic import BaseModel, Field

from app.config import TruthSettings
from app.services.truth.domain import AtomicClaim
from app.services.truth.evidence_matrix import (
    GAP_NO_CONTRADICTING,
    GAP_NO_EVIDENCE,
    GAP_NO_OFFICIAL_SOURCE,
    GAP_STALE_TIME_SENSITIVE,
)


QueryCategory = Literal["supporting", "contradicting", "official_source", "correction_retraction", "newer_information"]


class QueryPlanItem(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    category: QueryCategory


def generate_counter_evidence_queries(claim: AtomicClaim, settings: TruthSettings) -> list[QueryPlanItem]:
    """Deterministic query-variant plan for one atomic claim.

    Heuristic only, same discipline as Phase 3's deterministic_evidence_queries
    -- no model call here. One candidate per category (5 total) so a typical
    claim gets full coverage of all five query types by default; candidates
    are still generated in priority order (supporting first, newer-information
    last) and truncated to settings.max_queries_per_claim as a safety ceiling
    if that's ever configured below 5.

    Opinion and unverifiable claims aren't checkable, so they get no plan
    at all, matching Phase 3's deterministic_evidence_queries.
    """
    if claim.claim_type in ("opinion", "unverifiable"):
        return []

    claim_text = claim.normalized_claim
    candidates: list[QueryPlanItem] = [
        QueryPlanItem(query=claim_text, category="supporting"),
        QueryPlanItem(query=f"{claim_text} false", category="contradicting"),
        QueryPlanItem(
            query=f"{claim.entities[0]} official statement" if claim.entities else f"{claim_text} official statement",
            category="official_source",
        ),
        QueryPlanItem(query=f"{claim_text} correction", category="correction_retraction"),
        QueryPlanItem(
            query=f"{claim_text} since {claim.time_reference}" if claim.time_reference else f"{claim_text} latest update",
            category="newer_information",
        ),
    ]

    return candidates[: settings.max_queries_per_claim]


# Round-2 phrasing deliberately differs from round 1's query for the same
# category (round 1 uses "false"/"official statement"/"latest update") --
# a targeted retry that repeats the exact same query text against the same
# provider is unlikely to surface anything new.
_FOLLOWUP_BUILDERS: dict[str, tuple[QueryCategory, Callable[[AtomicClaim], str]]] = {
    GAP_NO_CONTRADICTING: ("contradicting", lambda claim: f"{claim.normalized_claim} debunked"),
    GAP_NO_OFFICIAL_SOURCE: ("official_source", lambda claim: f"{claim.normalized_claim} press release"),
    GAP_STALE_TIME_SENSITIVE: ("newer_information", lambda claim: f"{claim.normalized_claim} recent developments"),
}


def generate_followup_queries(claim: AtomicClaim, unresolved: list[str]) -> list[QueryPlanItem]:
    """Targeted, gap-specific round-2 query plan (Phase 6).

    Not every gap in ClaimEvidenceMatrix.unresolved has an actionable
    follow-up: single-domain corroboration, missing publication dates, and
    "everything landed in context" describe a quality problem with what was
    already found, not an untried category, so they're deliberately not
    mapped here. "No evidence at all" is handled separately -- there's no
    category-specific rephrasing to try when nothing came back at all, so
    this retries the single highest-priority (supporting) query once.
    """
    if claim.claim_type in ("opinion", "unverifiable"):
        # These never got a round-1 plan either (generate_counter_evidence_queries
        # skips them) -- the empty-evidence gap they trigger isn't a real gap
        # to fill, it's the expected, correct outcome for a non-checkable claim.
        return []
    if GAP_NO_EVIDENCE in unresolved:
        return [QueryPlanItem(query=claim.normalized_claim, category="supporting")]

    followups: list[QueryPlanItem] = []
    for gap in unresolved:
        mapping = _FOLLOWUP_BUILDERS.get(gap)
        if mapping is None:
            continue
        category, build_query = mapping
        followups.append(QueryPlanItem(query=build_query(claim), category=category))
    return followups
