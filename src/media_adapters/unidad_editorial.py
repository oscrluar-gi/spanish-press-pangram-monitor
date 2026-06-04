from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

from src.media_adapters.base import MediaAdapter


class UnidadEditorialAdapter(MediaAdapter):
    name = "unidad_editorial"
    use_wayback_rss = True
    canonical_host: str | None = None

    def normalize_discovery_url(self, url: str) -> str:
        if not self.canonical_host:
            return url
        split = urlsplit(url)
        host = split.netloc.lower()
        if host == self.canonical_host or host.endswith(".uecdn.es"):
            return url
        bare = self.canonical_host.removeprefix("www.")
        if host == bare:
            return urlunsplit((split.scheme or "https", self.canonical_host, split.path, split.query, split.fragment))
        return url

    def should_include_url(self, url: str) -> bool:
        path = urlsplit(url).path.lower()
        if "/videos/" in path or "/album/" in path or "/directo/" in path:
            return False
        return True


class ElMundoAdapter(UnidadEditorialAdapter):
    name = "elmundo"
    canonical_host = "www.elmundo.es"


class MarcaAdapter(UnidadEditorialAdapter):
    name = "marca"
    canonical_host = "www.marca.com"

    def should_include_url(self, url: str) -> bool:
        path = urlsplit(url).path.lower()
        if "/videos/" in path or "/video/" in path or "/album/" in path or "/marcador/" in path:
            return False
        return True


class ExpansionAdapter(UnidadEditorialAdapter):
    name = "expansion"
    canonical_host = "www.expansion.com"

    def should_include_url(self, url: str) -> bool:
        path = urlsplit(url).path.lower()
        if "/mercados/cotizaciones/" in path or "/diccionario-economico/" in path:
            return False
        return super().should_include_url(url)
