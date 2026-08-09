import hashlib
import re
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from app.services.truth.domain import Evidence
from app.services.truth.extractors import ExtractedPage
from app.services.truth.providers import SearchResult
from app.services.truth.security import validate_remote_url


TRACKING_QUERY_KEYS = {"fbclid", "gclid", "mc_cid", "mc_eid"}


def normalize_evidence_text(value: str) -> str:
    return " ".join(value.split())


def hash_evidence_content(value: str) -> str:
    return hashlib.sha256(normalize_evidence_text(value).encode("utf-8")).hexdigest()


def canonicalize_url(value: str) -> str:
    safe_url = validate_remote_url(value, resolve_dns=False)
    parsed = urlsplit(safe_url)
    hostname = (parsed.hostname or "").lower()
    host = f"[{hostname}]" if ":" in hostname else hostname
    if parsed.port and not ((parsed.scheme == "http" and parsed.port == 80) or (parsed.scheme == "https" and parsed.port == 443)):
        host = f"{host}:{parsed.port}"
    query_items = [
        (key, item)
        for key, item in parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() not in TRACKING_QUERY_KEYS and not key.lower().startswith("utm_")
    ]
    query = urlencode(sorted(query_items))
    return urlunsplit((parsed.scheme.lower(), host, parsed.path or "/", query, ""))


def make_evidence_id(claim_id: str, canonical_url: str, content_hash: str, provider: str) -> str:
    identity = f"{claim_id}\n{canonical_url}\n{content_hash}\n{provider}".encode("utf-8")
    return f"evidence_{hashlib.sha256(identity).hexdigest()[:24]}"


def make_claim_id(normalized_claim: str) -> str:
    digest = hashlib.sha256(normalize_evidence_text(normalized_claim).lower().encode("utf-8")).hexdigest()
    return f"claim_{digest[:16]}"


class EvidenceNormalizer:
    def normalize(
        self,
        claim_id: str,
        result: SearchResult,
        page: ExtractedPage,
        *,
        search_provider: str,
        extraction_provider: str,
    ) -> Evidence:
        passages = [normalize_evidence_text(item) for item in page.passages if normalize_evidence_text(item)]
        text = "\n\n".join(passages)
        if not text:
            raise ValueError("the extracted page contains no evidence text")
        snippet = normalize_evidence_text(result.snippet)
        chunk = snippet or passages[0]
        final_url = page.url or result.url
        canonical_url = canonicalize_url(final_url)
        digest = hash_evidence_content(text)
        published_at = result.published_at or page.published_at
        page_publisher = (page.publisher or "").strip()
        page_hostname = (urlsplit(page.url).hostname or "").lower()
        if page_publisher.lower() in {page_hostname, "unknown publisher"}:
            page_publisher = ""
        publisher = result.publisher or page_publisher or None
        return Evidence(
            evidence_id=make_evidence_id(claim_id, canonical_url, digest, search_provider),
            claim_id=claim_id,
            url=result.url,
            canonical_url=canonical_url,
            publisher=publisher,
            author=None,
            title=page.title or result.title or None,
            published_at=published_at,
            retrieved_at=page.retrieved_at,
            source_type="webpage",
            primary_or_secondary=None,
            text=text,
            evidence_chunk=chunk,
            provider=search_provider,
            content_hash=digest,
            metadata={
                "extraction_provider": extraction_provider,
                "final_url": final_url,
                "search_title": result.title,
                "search_snippet": result.snippet,
                "passages": passages,
                "published_at_raw": published_at,
            },
        )


@dataclass(frozen=True)
class DeduplicationResult:
    evidence: list[Evidence]
    duplicate_ids: dict[str, list[str]]


class EvidenceDeduplicator:
    """Deterministic URL/content deduplication; semantic similarity is intentionally out of scope."""

    def deduplicate(self, items: list[Evidence]) -> DeduplicationResult:
        unique: list[Evidence] = []
        duplicate_ids: dict[str, list[str]] = {}
        seen_urls: dict[str, str] = {}
        seen_hashes: dict[str, str] = {}

        for item in items:
            domain_group = (urlsplit(item.canonical_url).hostname or "").lower()
            publisher_group = re.sub(r"\s+", " ", (item.publisher or domain_group).strip().lower())
            enriched = item.model_copy(
                update={
                    "metadata": {
                        **item.metadata,
                        "domain_group": domain_group,
                        "publisher_group": publisher_group,
                    }
                }
            )
            duplicate_of = seen_urls.get(enriched.canonical_url) or seen_hashes.get(enriched.content_hash)
            if duplicate_of:
                duplicate_ids.setdefault(duplicate_of, []).append(enriched.evidence_id)
                continue
            seen_urls[enriched.canonical_url] = enriched.evidence_id
            seen_hashes[enriched.content_hash] = enriched.evidence_id
            unique.append(enriched)

        annotated = []
        for item in unique:
            duplicates = duplicate_ids.get(item.evidence_id, [])
            annotated.append(
                item.model_copy(
                    update={
                        "metadata": {
                            **item.metadata,
                            "exact_duplicate_ids": duplicates,
                            "exact_duplicate_count": len(duplicates),
                        }
                    }
                )
            )
        return DeduplicationResult(annotated, duplicate_ids)
