from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date
from typing import Any

import httpx

from app.config import TruthSettings
from app.services.truth.security import UnsafeUrlError, validate_remote_url


class SearchProviderError(RuntimeError):
    pass


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    snippet: str = ""
    published_at: str | None = None
    publisher: str | None = None


class SearchProvider(ABC):
    name: str

    @abstractmethod
    def search(self, claim: str, language: str, limit: int) -> list[SearchResult]:
        raise NotImplementedError


class SearXNGSearchProvider(SearchProvider):
    name = "searxng"

    def __init__(self, base_url: str, settings: TruthSettings):
        self.base_url = base_url
        self.settings = settings

    def search(self, claim: str, language: str, limit: int) -> list[SearchResult]:
        try:
            response = httpx.get(
                f"{self.base_url}/search",
                params={"q": claim, "format": "json", "language": language, "safesearch": 1, "categories": "general"},
                headers={"User-Agent": self.settings.user_agent, "Accept": "application/json"},
                timeout=self.settings.search_timeout_seconds,
                follow_redirects=False,
            )
            response.raise_for_status()
            payload: dict[str, Any] = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise SearchProviderError("SearXNG search failed") from exc

        results: list[SearchResult] = []
        for raw in payload.get("results", []):
            if len(results) >= limit:
                break
            try:
                safe_url = validate_remote_url(str(raw.get("url", "")), resolve_dns=True)
            except (UnsafeUrlError, ValueError):
                continue
            results.append(
                SearchResult(
                    title=str(raw.get("title") or safe_url)[:300],
                    url=safe_url,
                    snippet=str(raw.get("content") or "")[:1000],
                    published_at=str(raw.get("publishedDate") or raw.get("published_at") or "")[:40] or None,
                    publisher=None,
                )
            )
        return results


class DevelopmentMockSearchProvider(SearchProvider):
    name = "development_mock"

    def search(self, claim: str, language: str, limit: int) -> list[SearchResult]:
        del language
        lowered = claim.lower()
        if "scholarship" in lowered:
            results = [
                SearchResult("Official scholarship update", "https://example.org/truth-tone/scholarships", "Scholarships continue under the published programme.", str(date.today()), "Example University"),
                SearchResult("Student funding announcement", "https://example.org/truth-tone/funding", "Some scholarship categories were changed, but not all were cancelled.", str(date.today()), "Example University"),
            ]
        elif "moon" in lowered and "cheese" in lowered:
            results = [SearchResult("Moon composition overview", "https://example.org/truth-tone/moon", "Lunar samples are made of rock and minerals.", "2024-01-01", "Example Science Institute")]
        else:
            results = [SearchResult("Controlled evidence source", "https://example.org/truth-tone/general", "A controlled development source used to test the evidence flow.", "2025-01-01", "Example Research Institute")]
        return results[:limit]


def create_search_provider(settings: TruthSettings) -> SearchProvider:
    requested = settings.search_provider
    if requested == "searxng" or (requested == "auto" and settings.searxng_url):
        if settings.searxng_url:
            return SearXNGSearchProvider(settings.searxng_url, settings)
    return DevelopmentMockSearchProvider()
