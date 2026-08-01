import re

from app.models import ClaimResponse


OPINION_PATTERNS = (
    r"\bi (?:think|believe|feel|prefer)\b",
    r"\bin my opinion\b",
    r"\b(?:best|worst|beautiful|boring|delicious)\b",
)


def check_claim(text: str) -> ClaimResponse:
    claim = text.strip()
    if not claim:
        return ClaimResponse(
            status="invalid",
            claim="",
            explanation="Please enter a claim to check.",
            confidence="high",
            limitations="No claim was provided.",
            evidence=[],
        )

    if any(re.search(pattern, claim, re.IGNORECASE) for pattern in OPINION_PATTERNS):
        return ClaimResponse(
            status="opinion",
            claim=claim,
            explanation="This appears to express a personal opinion, so it cannot be verified as a factual claim.",
            confidence="medium",
            limitations="This milestone uses simple local keyword rules.",
            evidence=[],
        )

    return ClaimResponse(
        status="unverified",
        claim=claim,
        explanation="Real evidence retrieval has not been added yet. Unverified does not mean false.",
        confidence="low",
        limitations="This milestone uses local deterministic mock logic.",
        evidence=[],
    )
