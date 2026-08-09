from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

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
