from typing import Literal

from pydantic import BaseModel, Field

from app.config import TruthSettings
from app.services.truth.domain import AtomicClaim


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
