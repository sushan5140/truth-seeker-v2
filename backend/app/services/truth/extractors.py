import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib.util import find_spec
from urllib.parse import urlsplit

import httpx
from bs4 import BeautifulSoup

from app.config import TruthSettings
from app.services.truth.security import validate_redirect, validate_remote_url


class ExtractionError(RuntimeError):
    pass


@dataclass(frozen=True)
class ExtractedPage:
    title: str
    url: str
    publisher: str
    published_at: str | None
    retrieved_at: datetime
    passages: list[str]


class WebExtractor(ABC):
    name: str

    @abstractmethod
    def extract(self, url: str) -> ExtractedPage:
        raise NotImplementedError


def _clean_html(html: str, url: str) -> ExtractedPage:
    soup = BeautifulSoup(html, "lxml")
    for element in soup.select("script, style, noscript, form, nav, header, footer, aside, template, svg, canvas"):
        element.decompose()
    for element in soup.select('[hidden], [aria-hidden="true"], [style*="display:none" i], [style*="display: none" i]'):
        element.decompose()

    title = (soup.title.get_text(" ", strip=True) if soup.title else "")[:300] or urlsplit(url).hostname or "Source"
    published = None
    for selector, attribute in (
        ('meta[property="article:published_time"]', "content"),
        ('meta[name="date"]', "content"),
        ('meta[name="pubdate"]', "content"),
        ("time[datetime]", "datetime"),
    ):
        node = soup.select_one(selector)
        if node and node.get(attribute):
            published = str(node.get(attribute))[:40]
            break

    passages: list[str] = []
    for node in soup.select("main p, article p, main li, article li, body p, body li"):
        text = " ".join(node.get_text(" ", strip=True).split())
        if len(text) >= 40 and text not in passages:
            passages.append(text)
    return ExtractedPage(
        title=title,
        url=url,
        publisher=urlsplit(url).hostname or "Unknown publisher",
        published_at=published,
        retrieved_at=datetime.now(timezone.utc),
        passages=passages,
    )


class LightweightHttpExtractor(WebExtractor):
    name = "lightweight_http"
    allowed_content_types = ("text/html", "application/xhtml+xml", "text/plain")

    def __init__(self, settings: TruthSettings):
        self.settings = settings

    def extract(self, url: str) -> ExtractedPage:
        current = validate_remote_url(url, resolve_dns=True)
        headers = {"User-Agent": self.settings.user_agent, "Accept": "text/html,application/xhtml+xml,text/plain"}
        with httpx.Client(timeout=self.settings.extraction_timeout_seconds, follow_redirects=False, headers=headers) as client:
            for redirect_count in range(self.settings.max_redirects + 1):
                try:
                    with client.stream("GET", current) as response:
                        if response.status_code in {301, 302, 303, 307, 308}:
                            if redirect_count >= self.settings.max_redirects or not response.headers.get("location"):
                                raise ExtractionError("Too many or invalid redirects")
                            current = validate_redirect(current, response.headers["location"])
                            continue
                        response.raise_for_status()
                        content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
                        if content_type not in self.allowed_content_types:
                            raise ExtractionError("Unsupported content type")
                        chunks: list[bytes] = []
                        total = 0
                        for chunk in response.iter_bytes():
                            total += len(chunk)
                            if total > self.settings.max_response_bytes:
                                raise ExtractionError("Source response is too large")
                            chunks.append(chunk)
                        html = b"".join(chunks).decode(response.encoding or "utf-8", errors="replace")
                        return _clean_html(html, current)
                except httpx.HTTPError as exc:
                    raise ExtractionError("Source download failed") from exc
        raise ExtractionError("Source extraction failed")


class DevelopmentMockExtractor(WebExtractor):
    name = "development_mock"

    PAGES = {
        "/truth-tone/scholarships": """<html><head><title>Official scholarship update</title><meta property='article:published_time' content='2026-07-01'></head><body><main><p>Example University continues to offer international scholarships under its published funding programme. No announcement cancelling all international scholarships has been issued.</p></main></body></html>""",
        "/truth-tone/funding": """<html><head><title>Student funding announcement</title></head><body><article><p>The university changed eligibility for two funding categories. Other international scholarship programmes remain open for applications.</p></article></body></html>""",
        "/truth-tone/moon": """<html><head><title>Moon composition overview</title></head><body><main><p>Analysis of lunar samples shows that the Moon is composed of rock, minerals, and glass formed by impacts and volcanic activity, not cheese.</p></main></body></html>""",
        "/truth-tone/general": """<html><head><title>Controlled evidence source</title></head><body><main><p>This controlled development passage confirms that the manual retrieval, extraction, ranking, and citation interface is operating without claiming live web research.</p></main></body></html>""",
    }

    def extract(self, url: str) -> ExtractedPage:
        safe_url = validate_remote_url(url, resolve_dns=False)
        html = self.PAGES.get(urlsplit(safe_url).path)
        if not html:
            raise ExtractionError("No controlled mock page exists")
        return _clean_html(html, safe_url)


class Crawl4AIExtractor(WebExtractor):
    name = "crawl4ai"

    def extract(self, url: str) -> ExtractedPage:
        safe_url = validate_remote_url(url, resolve_dns=True)

        async def crawl() -> ExtractedPage:
            from crawl4ai import AsyncWebCrawler

            async with AsyncWebCrawler() as crawler:
                result = await crawler.arun(url=safe_url)
                if not result.success:
                    raise ExtractionError("Crawl4AI failed")
                return _clean_html(result.html or result.cleaned_html or "", safe_url)

        return asyncio.run(crawl())


def create_extractor(settings: TruthSettings, search_provider_name: str) -> WebExtractor:
    requested = settings.extraction_provider
    if requested == "crawl4ai" and find_spec("crawl4ai") is not None:
        return Crawl4AIExtractor()
    if requested == "mock" or (requested == "auto" and search_provider_name == "development_mock"):
        return DevelopmentMockExtractor()
    return LightweightHttpExtractor(settings)
