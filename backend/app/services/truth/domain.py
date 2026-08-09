import hashlib
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.services.truth.security import validate_remote_url


ClaimType = Literal[
    "factual",
    "opinion",
    "prediction",
    "quotation",
    "statistic",
    "historical",
    "current_event",
    "scientific",
    "unverifiable",
    "other",
]
PrimaryOrSecondary = Literal["primary", "secondary"]
InternalVerdict = Literal[
    "SUPPORTED",
    "CONTRADICTED",
    "MISLEADING",
    "MISSING_CONTEXT",
    "DISPUTED",
    "OUTDATED",
    "INSUFFICIENT_EVIDENCE",
    "UNVERIFIABLE",
]


class DomainModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AtomicClaim(DomainModel):
    claim_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")
    original_text: str = Field(min_length=1, max_length=5000)
    normalized_claim: str = Field(min_length=1, max_length=5000)
    claim_type: ClaimType
    entities: list[str] = Field(default_factory=list, max_length=100)
    location: str | None = Field(default=None, max_length=300)
    time_reference: str | None = Field(default=None, max_length=300)
    time_sensitive: bool = False
    verification_requirements: list[str] = Field(default_factory=list, max_length=50)

    @field_validator("original_text", "normalized_claim")
    @classmethod
    def text_must_not_be_blank(cls, value: str) -> str:
        cleaned = " ".join(value.split())
        if not cleaned:
            raise ValueError("claim text must not be blank")
        return cleaned


class Evidence(DomainModel):
    evidence_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    claim_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")
    url: str = Field(min_length=1, max_length=2048)
    canonical_url: str = Field(min_length=1, max_length=2048)
    publisher: str | None = Field(default=None, max_length=300)
    author: str | None = Field(default=None, max_length=300)
    title: str | None = Field(default=None, max_length=500)
    published_at: str | None = Field(default=None, max_length=80)
    retrieved_at: datetime
    source_type: str | None = Field(default=None, max_length=80)
    primary_or_secondary: PrimaryOrSecondary | None = None
    text: str = Field(min_length=1)
    evidence_chunk: str = Field(min_length=1)
    provider: str = Field(min_length=1, max_length=100)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("url")
    @classmethod
    def original_url_must_be_safe(cls, value: str) -> str:
        cleaned = value.strip()
        validate_remote_url(cleaned, resolve_dns=False)
        return cleaned

    @field_validator("canonical_url")
    @classmethod
    def canonical_url_must_be_safe(cls, value: str) -> str:
        return validate_remote_url(value, resolve_dns=False)

    @field_validator("text", "evidence_chunk")
    @classmethod
    def evidence_text_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("evidence text must not be blank")
        return value.strip()

    @model_validator(mode="after")
    def content_hash_must_match_text(self) -> "Evidence":
        normalized = " ".join(self.text.split())
        expected = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        if self.content_hash != expected:
            raise ValueError("content_hash must match normalized evidence text")
        return self


class ProviderError(DomainModel):
    code: str = Field(min_length=1, max_length=100)
    message: str = Field(min_length=1, max_length=500)
    retryable: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class ProviderResult(DomainModel):
    success: bool
    provider: str = Field(min_length=1, max_length=100)
    evidence: list[Evidence] = Field(default_factory=list)
    errors: list[ProviderError] = Field(default_factory=list)
    latency_ms: float = Field(ge=0)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def failed_result_needs_an_error(self) -> "ProviderResult":
        if not self.success and not self.errors:
            raise ValueError("a failed provider result must include at least one error")
        return self


class ClaimEvidenceMatrix(DomainModel):
    claim: AtomicClaim
    supporting: list[Evidence] = Field(default_factory=list)
    contradicting: list[Evidence] = Field(default_factory=list)
    context: list[Evidence] = Field(default_factory=list)
    unresolved: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def evidence_must_belong_to_claim(self) -> "ClaimEvidenceMatrix":
        items = [*self.supporting, *self.contradicting, *self.context]
        if any(item.claim_id != self.claim.claim_id for item in items):
            raise ValueError("matrix evidence must belong to its atomic claim")
        return self


class VerificationResult(DomainModel):
    claim: AtomicClaim
    verdict: InternalVerdict
    rationale: str = Field(min_length=1, max_length=4000)
    evidence_ids: list[str] = Field(default_factory=list)
    matrix: ClaimEvidenceMatrix | None = None
    confidence_score: float | None = Field(default=None, ge=0, le=1)
    confidence_type: Literal["heuristic_evidence_confidence"] | None = None
    verified_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def matrix_must_match_claim(self) -> "VerificationResult":
        if self.matrix and self.matrix.claim.claim_id != self.claim.claim_id:
            raise ValueError("verification matrix must match the verified claim")
        if self.confidence_score is not None and self.confidence_type is None:
            raise ValueError("confidence_type is required when confidence_score is present")
        return self
