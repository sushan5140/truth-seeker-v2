from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from app.services.truth.domain import InternalVerdict


ToneSensitivity = Literal["low", "balanced", "high"]
Verdict = Literal["supported", "contradicted", "mixed", "insufficient_evidence", "opinion", "invalid"]
Confidence = Literal["low", "medium", "high"]
SourceCategory = Literal[
    "official_government",
    "official_institution",
    "university_or_regulator",
    "primary_document",
    "recognised_news",
    "fact_check",
    "academic",
    "general",
]
EvidenceStance = Literal["supports", "contradicts", "mixed", "neutral"]


class TextRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=5000)


class ToneRequest(TextRequest):
    sensitivity: ToneSensitivity = "balanced"


class ClaimRequest(TextRequest):
    source_url: str | None = Field(default=None, max_length=2048)
    page_title: str | None = Field(default=None, max_length=300)
    language: str = Field(default="en", min_length=2, max_length=16)
    bypass_cache: bool = False

    @field_validator("source_url")
    @classmethod
    def source_url_must_be_remote_http(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        from app.services.truth.security import validate_remote_url

        return validate_remote_url(value, resolve_dns=False)


class EvidenceItem(BaseModel):
    title: str
    url: str
    publisher: str
    published_at: str | None = None
    retrieved_at: datetime
    stance: EvidenceStance
    excerpt: str
    source_category: SourceCategory
    ranking_score: float = Field(ge=0, le=100)

    @field_validator("url")
    @classmethod
    def evidence_url_must_be_safe(cls, value: str) -> str:
        from app.services.truth.security import validate_remote_url

        return validate_remote_url(value, resolve_dns=False)


class ProviderInfo(BaseModel):
    search: str
    extractor: str
    verifier: str


class ClaimResponse(BaseModel):
    verdict: Verdict
    claim: str
    confidence: Confidence
    summary: str
    limitations: list[str]
    evidence: list[EvidenceItem]
    provider: ProviderInfo
    cached: bool
    checked_at: datetime
    multiple_claims_detected: bool = False
    certainty_notice: str = "Verdicts reflect the evidence found from the sources searched, not absolute certainty."


class TruthStatusResponse(BaseModel):
    backend_status: Literal["running"]
    gemini_configured: bool
    search_provider_configured: str
    extraction_provider_configured: str
    cache_available: bool


class ToneResponse(BaseModel):
    intervention_needed: bool
    confidence: Confidence
    category: str
    problematic_phrase: str | None
    suggested_rewrite: str
    explanation: str
    provider: Literal["gemini", "local_fallback"]


class GeminiToneResult(BaseModel):
    intervention_needed: bool
    category: str
    confidence: Confidence
    problematic_phrase: str | None
    suggestion: str
    explanation: str


class GeminiClaimExtraction(BaseModel):
    kind: Literal["factual", "opinion", "question", "invalid"]
    main_claim: str
    multiple_claims_detected: bool


class GeminiEvidenceAssessment(BaseModel):
    verdict: Verdict
    confidence: Confidence
    summary: str
    source_stances: list[EvidenceStance]


class GeminiMatrixAssessment(BaseModel):
    """Structured model output for /api/v2/verify's claim-evidence-matrix
    verdict (Phase 7). Shared by GeminiEvidenceVerifier and NemotronVerifier
    -- same naming precedent as GeminiEvidenceAssessment already being reused
    by NemotronVerifier.compare() since Phase 2."""

    verdict: InternalVerdict
    rationale: str = Field(min_length=1, max_length=4000)
    confidence_score: float | None = Field(default=None, ge=0, le=1)
