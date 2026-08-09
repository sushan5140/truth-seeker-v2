from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from app.services.truth.domain import AtomicClaim, ClaimEvidenceMatrix, VerificationResult

if TYPE_CHECKING:
    from app.services.truth.ranking import RankedSource


@dataclass(frozen=True)
class ProcessedClaim:
    kind: str
    main_claim: str
    multiple_claims_detected: bool
    time_sensitive: bool


class VerificationModel(ABC):
    """Provider-neutral boundary for the active verifier and future replacements."""

    @property
    @abstractmethod
    def configured(self) -> bool:
        raise NotImplementedError

    @abstractmethod
    def extract_claim(self, text: str) -> ProcessedClaim:
        raise NotImplementedError

    @abstractmethod
    def compare(self, claim: str, evidence: list["RankedSource"]) -> Any:
        raise NotImplementedError

    @abstractmethod
    def assess_matrix(self, claim: AtomicClaim, matrix: ClaimEvidenceMatrix, *, rounds: int) -> VerificationResult:
        """Phase 7: reason over a Phase 5 ClaimEvidenceMatrix (v2's evidence
        shape) into a verdict. Distinct from compare(), which reasons over
        v1's list[RankedSource] -- the two evidence shapes aren't
        interchangeable, so this is a new method rather than an overload.
        Must raise (not return a guessed verdict) if not configured or if
        the underlying call fails; callers fall back to a deterministic
        heuristic on any exception, same pattern as compare()."""
        raise NotImplementedError
