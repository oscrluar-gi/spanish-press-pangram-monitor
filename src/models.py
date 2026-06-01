from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class MediaConfig:
    name: str
    domain: str
    sitemap_urls: list[str] = field(default_factory=list)
    sitemap_page_range: tuple[int, int] | None = None
    rss_feeds: list[str] = field(default_factory=list)
    include_url_patterns: list[str] = field(default_factory=list)
    exclude_url_patterns: list[str] = field(default_factory=list)
    include_liveblogs: bool = False
    include_opinion: bool = True
    include_sports: bool = True
    allow_month_fallback: bool = False
    wayback_discovery: bool = False
    wayback_discovery_min_candidates: int | None = None
    wayback_discovery_limit: int | None = None
    wayback_discovery_patterns: list[str] = field(default_factory=list)
    wayback_discovery_broad: bool = False
    gdelt_discovery: bool = True
    gdelt_discovery_limit: int | None = None
    request_delay_seconds: float | None = None
    max_concurrency_per_domain: int | None = None
    max_retries: int | None = None


@dataclass(frozen=True)
class WaybackConfig:
    enabled: bool = True
    strategy: str = "fallback_only"
    max_days_after: int = 7
    max_days_before: int = 1
    request_delay_seconds: float = 1.5
    max_retries: int = 2
    timeout_seconds: float = 45.0
    use_cdx: bool = True
    use_availability: bool = True
    extended_search: bool = True
    extended_max_days_after: int = 30
    extended_max_days_before: int = 7
    discovery_enabled: bool = True
    discovery_max_urls_per_media: int = 250
    discovery_max_days_after: int = 3
    discovery_max_days_before: int = 0
    discovery_timeout_seconds: float = 15.0
    discovery_max_retries: int = 0
    discovery_min_candidates: int = 2


@dataclass(frozen=True)
class GdeltConfig:
    enabled: bool = True
    max_results_per_media: int = 100
    request_delay_seconds: float = 12.0
    max_retries: int = 2
    retry_delay_seconds: float = 15.0
    timeout_seconds: float = 30.0


@dataclass(frozen=True)
class DiscoveredURL:
    media_name: str
    domain: str
    url: str
    discovered_from: str
    target_date: str
    discovered_lastmod: str | None = None
    rss_published_at: str | None = None
    source_type: str = "sitemap"
    filter_status: str = "included"
    filter_reason: str | None = None

    @property
    def lastmod(self) -> str | None:
        return self.discovered_lastmod


@dataclass(frozen=True)
class ExtractedArticle:
    url: str
    normalized_url: str
    target_date: str
    title: str | None
    author: str | None
    article_published_at: str | None
    article_modified_at: str | None
    section: str | None
    tags: list[str]
    canonical_url: str | None
    language: str | None
    is_paywalled: bool
    text_clean: str | None
    word_count: int
    text_hash: str | None
    extraction_status: str
    error: str | None = None
    content_source: str = "live"
    source_url: str | None = None
    original_url: str | None = None
    wayback_timestamp: str | None = None
    wayback_distance_seconds: int | None = None
    wayback_statuscode: str | None = None
    wayback_mimetype: str | None = None
    wayback_digest: str | None = None
    wayback_source_api: str | None = None

    @property
    def published_at(self) -> str | None:
        return self.article_published_at
