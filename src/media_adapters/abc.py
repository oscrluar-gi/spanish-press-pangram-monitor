from __future__ import annotations

from datetime import date
from urllib.parse import urlsplit, urlunsplit

from src.media_adapters.base import MediaAdapter
from src.models import MediaConfig


class AbcAdapter(MediaAdapter):
    name = "abc"

    def extra_sitemap_urls(self, media: MediaConfig, target: date) -> list[str]:
        return [
            "https://www.abc.es/sitemap-index.xml",
            "https://www.abc.es/sitemap_news.xml",
            "https://www.abc.es/sitemap.xml",
        ]

    def normalize_discovery_url(self, url: str) -> str:
        split = urlsplit(url)
        host = split.netloc.lower()
        if host == "abc.es":
            return urlunsplit((split.scheme or "https", "www.abc.es", split.path, split.query, split.fragment))
        return url

    def should_include_url(self, url: str) -> bool:
        path = urlsplit(url).path.lower()
        if path.endswith("-ga.html"):
            return False
        return True
