from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING

from src.models import MediaConfig

if TYPE_CHECKING:
    from src.discover import SitemapEntry


class MediaAdapter:
    name = "base"
    use_wayback_rss = False

    def extra_sitemap_urls(self, media: MediaConfig, target: date) -> list[str]:
        return []

    def extra_rss_feeds(self, media: MediaConfig, target: date) -> list[str]:
        return []

    def filter_sitemap_index_entries(self, entries: list[SitemapEntry], target: date) -> list[SitemapEntry]:
        return entries

    def entry_matches_target(
        self,
        entry: SitemapEntry,
        target: date,
        time_window: tuple[datetime, datetime] | None,
        allow_month_fallback: bool,
    ) -> bool | None:
        return None

    def extra_wayback_discovery_patterns(self, media: MediaConfig, target: date) -> list[str]:
        return []

    def normalize_discovery_url(self, url: str) -> str:
        return url

    def should_include_url(self, url: str) -> bool:
        return True

    def metadata_trace(self, media: MediaConfig) -> dict[str, str]:
        return {
            "adapter": self.name,
            "canonical_domain": media.canonical_host,
            "gdelt_domain": media.gdelt_host,
        }
