from typing import Literal

from pydantic import BaseModel, Field


ToneSensitivity = Literal["low", "balanced", "high"]


class TextRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=5000)


class ToneRequest(TextRequest):
    sensitivity: ToneSensitivity = "balanced"


class ClaimResponse(BaseModel):
    status: Literal["supported", "contradicted", "unverified", "no_evidence_found", "opinion", "invalid"]
    claim: str
    explanation: str
    confidence: Literal["low", "medium", "high"]
    limitations: str
    evidence: list[str]


class ToneResponse(BaseModel):
    intervention_needed: bool
    confidence: Literal["low", "medium", "high"]
    category: str
    problematic_phrase: str | None
    suggested_rewrite: str
    explanation: str
    provider: Literal["gemini", "local_fallback"]


class GeminiToneResult(BaseModel):
    intervention_needed: bool
    category: str
    confidence: Literal["low", "medium", "high"]
    problematic_phrase: str | None
    suggestion: str
    explanation: str
