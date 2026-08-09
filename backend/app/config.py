import os
from dataclasses import dataclass
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]


def _positive_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default))))
    except ValueError:
        return default


@dataclass(frozen=True)
class TruthSettings:
    search_provider: str = os.getenv("TRUTH_SEARCH_PROVIDER", "auto").strip().lower()
    extraction_provider: str = os.getenv("TRUTH_EXTRACTION_PROVIDER", "auto").strip().lower()
    searxng_url: str = os.getenv("SEARXNG_URL", "").strip().rstrip("/")
    cache_path: Path = Path(os.getenv("TRUTH_CACHE_PATH", str(BACKEND_DIR / "data" / "claim_cache.sqlite3")))
    max_search_results: int = _positive_int("TRUTH_MAX_SEARCH_RESULTS", 5)
    max_evidence_sources: int = _positive_int("TRUTH_MAX_EVIDENCE_SOURCES", 3)
    max_passages_per_source: int = _positive_int("TRUTH_MAX_PASSAGES_PER_SOURCE", 2)
    max_passage_length: int = _positive_int("TRUTH_MAX_PASSAGE_LENGTH", 700)
    max_response_bytes: int = _positive_int("TRUTH_MAX_RESPONSE_BYTES", 1_500_000)
    max_redirects: int = _positive_int("TRUTH_MAX_REDIRECTS", 3)
    search_timeout_seconds: float = float(os.getenv("TRUTH_SEARCH_TIMEOUT_SECONDS", "5"))
    extraction_timeout_seconds: float = float(os.getenv("TRUTH_EXTRACTION_TIMEOUT_SECONDS", "7"))
    gemini_timeout_seconds: float = float(os.getenv("TRUTH_GEMINI_TIMEOUT_SECONDS", "8"))
    duplicate_wait_seconds: float = float(os.getenv("TRUTH_DUPLICATE_WAIT_SECONDS", "15"))
    ordinary_cache_hours: int = _positive_int("TRUTH_ORDINARY_CACHE_HOURS", 72)
    time_sensitive_cache_minutes: int = _positive_int("TRUTH_TIME_SENSITIVE_CACHE_MINUTES", 30)
    reasoning_model: str | None = os.getenv("TRUTHSEEKER_REASONING_MODEL", "").strip() or None
    verifier_model: str | None = os.getenv("TRUTHSEEKER_VERIFIER_MODEL", "").strip() or None
    embedding_model: str | None = os.getenv("TRUTHSEEKER_EMBEDDING_MODEL", "").strip() or None
    reranker_model: str | None = os.getenv("TRUTHSEEKER_RERANKER_MODEL", "").strip() or None
    openrouter_base_url: str | None = os.getenv("OPENROUTER_BASE_URL", "").strip() or None
    openrouter_timeout_seconds: float = float(os.getenv("TRUTH_OPENROUTER_TIMEOUT_SECONDS", "8"))
    max_queries_per_claim: int = _positive_int("TRUTH_MAX_QUERIES_PER_CLAIM", 6)
    classifier_model: str | None = os.getenv("TRUTHSEEKER_CLASSIFIER_MODEL", "").strip() or None
    second_round_timeout_seconds: float = float(os.getenv("TRUTH_SECOND_ROUND_TIMEOUT_SECONDS", "10"))
    user_agent: str = "TruthToneGuardian/2.0 (+local research assistant)"


SETTINGS = TruthSettings()
