import re
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlsplit

from app.config import TruthSettings
from app.models import SourceCategory
from app.services.truth.domain import Evidence


CATEGORY_SCORE: dict[SourceCategory, float] = {
    "official_government": 30,
    "official_institution": 27,
    "university_or_regulator": 25,
    "primary_document": 24,
    "recognised_news": 20,
    "fact_check": 19,
    "academic": 18,
    "general": 10,
}
NEWS_DOMAINS = {"reuters.com", "apnews.com", "bbc.com", "bbc.co.uk", "npr.org", "theguardian.com"}
FACT_CHECK_DOMAINS = {"snopes.com", "factcheck.org", "politifact.com", "fullfact.org"}
ACADEMIC_DOMAINS = {"doi.org", "arxiv.org", "pubmed.ncbi.nlm.nih.gov", "nature.com", "science.org"}
INSTITUTIONAL_DOMAINS = {"who.int", "un.org", "worldbank.org", "europa.eu", "nasa.gov"}
WORD_PATTERN = re.compile(r"[a-z0-9']+", re.IGNORECASE)


@dataclass(frozen=True)
class RankedSource:
    evidence: Evidence
    category: SourceCategory
    passages: list[str]
    score: float


def source_category(url: str) -> SourceCategory:
    host = (urlsplit(url).hostname or "").lower()
    if host.endswith(".gov") or ".gov." in host:
        return "official_government"
    if host.endswith(".edu") or host.endswith(".ac.uk") or "regulator" in host:
        return "university_or_regulator"
    if host in INSTITUTIONAL_DOMAINS or any(host.endswith(f".{domain}") for domain in INSTITUTIONAL_DOMAINS):
        return "official_institution"
    if host in NEWS_DOMAINS or any(host.endswith(f".{domain}") for domain in NEWS_DOMAINS):
        return "recognised_news"
    if host in FACT_CHECK_DOMAINS or any(host.endswith(f".{domain}") for domain in FACT_CHECK_DOMAINS):
        return "fact_check"
    if host in ACADEMIC_DOMAINS or any(host.endswith(f".{domain}") for domain in ACADEMIC_DOMAINS):
        return "academic"
    if any(token in url.lower() for token in ("/press-release", "/report", "/document", ".pdf")):
        return "primary_document"
    return "general"


def _tokens(text: str) -> set[str]:
    return {word.lower() for word in WORD_PATTERN.findall(text) if len(word) > 2}


def _passage_score(claim_tokens: set[str], passage: str) -> float:
    passage_tokens = _tokens(passage)
    if not claim_tokens or not passage_tokens:
        return 0
    overlap = len(claim_tokens & passage_tokens) / len(claim_tokens)
    explicit = 0.08 if re.search(r"\b(?:confirm|confirmed|continues?|remain|cancelled|false|not|no evidence|according to)\b", passage, re.IGNORECASE) else 0
    return min(1.0, overlap + explicit)


def _recency_score(published_at: str | None, time_sensitive: bool) -> float:
    if not time_sensitive or not published_at:
        return 5 if published_at else 0
    match = re.search(r"\b(20\d{2})\b", published_at)
    if not match:
        return 0
    age = max(0, datetime.now(timezone.utc).year - int(match.group(1)))
    return max(0, 15 - age * 3)


def rank_evidence(
    claim: str,
    sources: list[Evidence],
    settings: TruthSettings,
    *,
    time_sensitive: bool,
) -> list[RankedSource]:
    claim_tokens = _tokens(claim)
    ranked: list[RankedSource] = []
    domains: set[str] = set()

    for evidence in sources:
        raw_passages = evidence.metadata.get("passages")
        passages = raw_passages if isinstance(raw_passages, list) else [evidence.text]
        scored_passages = sorted(
            ((_passage_score(claim_tokens, str(passage)), str(passage)) for passage in passages),
            key=lambda item: item[0],
            reverse=True,
        )
        selected = [passage[: settings.max_passage_length] for score, passage in scored_passages if score > 0][: settings.max_passages_per_source]
        if not selected:
            continue

        category = source_category(evidence.canonical_url)
        domain = str(evidence.metadata.get("domain_group") or urlsplit(evidence.canonical_url).hostname or "")
        independence = 0 if domain in domains else 8
        domains.add(domain)
        relevance = min(45, max(score for score, _ in scored_passages) * 45)
        score = min(100, relevance + CATEGORY_SCORE[category] + _recency_score(evidence.published_at, time_sensitive) + independence)
        ranked.append(RankedSource(evidence, category, selected, round(score, 1)))

    return sorted(ranked, key=lambda item: item.score, reverse=True)[: settings.max_evidence_sources]
