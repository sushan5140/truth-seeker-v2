import asyncio
import time
from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.config import TruthSettings
from app.services.truth.domain import Evidence, ProviderError, ProviderResult
from app.services.truth.evidence_processing import EvidenceNormalizer
from app.services.truth.extractors import ExtractionError, ExtractionTimeoutError, WebExtractor
from app.services.truth.providers import SearchProvider, SearchProviderError, SearchProviderTimeout, SearchResult
from app.services.truth.security import UnsafeUrlError, validate_remote_url


class EvidenceSearchContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claim_id: str = Field(min_length=1, max_length=100)
    language: str = Field(default="en", min_length=2, max_length=16)
    limit: int = Field(default=5, ge=1, le=100)
    metadata: dict[str, Any] = Field(default_factory=dict)


class EvidenceProvider(ABC):
    name: str

    @abstractmethod
    def search_sync(self, query: str, context: EvidenceSearchContext) -> ProviderResult:
        raise NotImplementedError

    async def search(self, query: str, context: EvidenceSearchContext) -> ProviderResult:
        return await asyncio.to_thread(self.search_sync, query, context)


class WebExtractionProvider:
    """Preserves the existing extractor and URL-security behavior behind one boundary."""

    def __init__(self, extractor: WebExtractor, *, resolve_dns: bool):
        self.extractor = extractor
        self.resolve_dns = resolve_dns
        self.normalizer = EvidenceNormalizer()

    def extract(
        self,
        result: SearchResult,
        *,
        claim_id: str,
        search_provider: str,
    ) -> tuple[Evidence | None, ProviderError | None]:
        try:
            safe_url = validate_remote_url(result.url, resolve_dns=self.resolve_dns)
            safe_result = SearchResult(result.title, safe_url, result.snippet, result.published_at, result.publisher)
            page = self.extractor.extract(safe_url)
            evidence = self.normalizer.normalize(
                claim_id,
                safe_result,
                page,
                search_provider=search_provider,
                extraction_provider=self.extractor.name,
            )
            return evidence, None
        except (TimeoutError, ExtractionTimeoutError):
            return None, ProviderError(code="EXTRACTION_TIMEOUT", message="Source extraction timed out.", retryable=True)
        except (UnsafeUrlError, ExtractionError, ValueError):
            return None, ProviderError(code="EXTRACTION_FAILED", message="A search result could not be safely extracted.")


class LegacyRetrievalEvidenceProvider(EvidenceProvider):
    def __init__(self, search_provider: SearchProvider, extractor: WebExtractor):
        self.search_provider = search_provider
        self.name = search_provider.name
        self.extraction = WebExtractionProvider(extractor, resolve_dns=search_provider.name != "development_mock")

    def search_sync(self, query: str, context: EvidenceSearchContext) -> ProviderResult:
        started = time.perf_counter()
        try:
            results = self.search_provider.search(query, context.language, context.limit)[: context.limit]
        except (TimeoutError, SearchProviderTimeout):
            return ProviderResult(
                success=False,
                provider=self.name,
                errors=[ProviderError(code="PROVIDER_TIMEOUT", message="The search provider timed out.", retryable=True)],
                latency_ms=(time.perf_counter() - started) * 1000,
            )
        except SearchProviderError:
            return ProviderResult(
                success=False,
                provider=self.name,
                errors=[ProviderError(code="SEARCH_PROVIDER_UNAVAILABLE", message="The search provider was unavailable.", retryable=True)],
                latency_ms=(time.perf_counter() - started) * 1000,
            )
        except Exception:
            return ProviderResult(
                success=False,
                provider=self.name,
                errors=[ProviderError(code="PROVIDER_FAILURE", message="The search provider failed.")],
                latency_ms=(time.perf_counter() - started) * 1000,
            )

        evidence: list[Evidence] = []
        errors: list[ProviderError] = []
        for result in results:
            item, error = self.extraction.extract(result, claim_id=context.claim_id, search_provider=self.name)
            if item:
                evidence.append(item)
            if error:
                errors.append(error)
        return ProviderResult(
            success=True,
            provider=self.name,
            evidence=evidence,
            errors=errors,
            latency_ms=(time.perf_counter() - started) * 1000,
            metadata={"results_received": len(results), "documents_extracted": len(evidence)},
        )


class SearXNGEvidenceProvider(LegacyRetrievalEvidenceProvider):
    pass


class MockEvidenceProvider(LegacyRetrievalEvidenceProvider):
    pass


def create_evidence_provider(
    settings: TruthSettings,
    search_provider: SearchProvider,
    extractor: WebExtractor,
) -> EvidenceProvider:
    del settings
    if search_provider.name == "searxng":
        return SearXNGEvidenceProvider(search_provider, extractor)
    if search_provider.name == "development_mock":
        return MockEvidenceProvider(search_provider, extractor)
    return LegacyRetrievalEvidenceProvider(search_provider, extractor)
