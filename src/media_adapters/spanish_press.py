from __future__ import annotations

from datetime import date
from urllib.parse import urlsplit, urlunsplit

from src.media_adapters.base import MediaAdapter
from src.models import MediaConfig

if False:
    from src.discover import SitemapEntry


def _target_month_token(target: date) -> str:
    return f"{target.year:04d}_{target.month:02d}"


def _target_compact_month_token(target: date) -> str:
    return f"{target.year:04d}{target.month:02d}"


class WwwCanonicalAdapter(MediaAdapter):
    canonical_host: str | None = None

    def normalize_discovery_url(self, url: str) -> str:
        if not self.canonical_host:
            return url
        split = urlsplit(url)
        host = split.netloc.lower()
        bare = self.canonical_host.removeprefix("www.")
        if host == bare:
            return urlunsplit((split.scheme or "https", self.canonical_host, split.path, split.query, split.fragment))
        return url


class LavanguardiaAdapter(WwwCanonicalAdapter):
    name = "lavanguardia"
    canonical_host = "www.lavanguardia.com"
    use_wayback_rss = True

    def extra_rss_feeds(self, media: MediaConfig, target: date) -> list[str]:
        return ["https://www.lavanguardia.com/rss/home.xml"]

    def extra_sitemap_urls(self, media: MediaConfig, target: date) -> list[str]:
        return ["https://www.lavanguardia.com/sitemap-google-news.xml"]

    def should_include_url(self, url: str) -> bool:
        path = urlsplit(url).path.lower()
        if "/participacion/las-fotos-de-los-lectores/" in path or "/fotos/" in path:
            return False
        return True


class EldiarioAdapter(WwwCanonicalAdapter):
    name = "eldiario"
    canonical_host = "www.eldiario.es"

    def filter_sitemap_index_entries(self, entries: list["SitemapEntry"], target: date) -> list["SitemapEntry"]:
        month_token = _target_month_token(target)
        return [
            entry
            for entry in entries
            if "sitemap_google_news" in entry.loc
            or f"sitemap_contents_{month_token}" in entry.loc
        ]

    def should_include_url(self, url: str) -> bool:
        path = urlsplit(url).path.lower()
        if "/vertele/lo-mas-visto" in path or "/api/" in path:
            return False
        return True


class PublicoAdapter(WwwCanonicalAdapter):
    name = "publico"
    canonical_host = "www.publico.es"
    use_wayback_rss = True

    def filter_sitemap_index_entries(self, entries: list["SitemapEntry"], target: date) -> list["SitemapEntry"]:
        month_token = _target_compact_month_token(target)
        return [
            entry
            for entry in entries
            if "sitemap-google-news" in entry.loc
            or f"sitemap-noticias-{month_token}" in entry.loc
        ]

    def should_include_url(self, url: str) -> bool:
        path = urlsplit(url).path.lower()
        if "/sitemap-category" in path or "/sitemap-tag" in path:
            return False
        return True


class ElConfidencialAdapter(WwwCanonicalAdapter):
    name = "elconfidencial"
    canonical_host = "www.elconfidencial.com"
    use_wayback_rss = True

    def should_include_url(self, url: str) -> bool:
        path = urlsplit(url).path.lower()
        if "/multimedia/fotos/" in path or "/ultima-hora-en-vivo/" in path or "/archivo/" in path:
            return False
        return True


class LaRazonAdapter(WwwCanonicalAdapter):
    name = "larazon"
    canonical_host = "www.larazon.es"
    use_wayback_rss = True

    def should_include_url(self, url: str) -> bool:
        path = urlsplit(url).path.lower()
        if path.startswith("/pf/") or "/json/" in path or "/buscador/" in path:
            return False
        return True


class EleconomistaAdapter(WwwCanonicalAdapter):
    name = "eleconomista"
    canonical_host = "www.eleconomista.es"
    use_wayback_rss = True

    def should_include_url(self, url: str) -> bool:
        path = urlsplit(url).path.lower()
        if "/mercados-cotizaciones/" in path or "/diccionario/" in path or "/ranking/" in path:
            return False
        return True
