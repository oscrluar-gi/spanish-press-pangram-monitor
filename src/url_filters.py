from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from src.models import MediaConfig

DEFAULT_EXCLUDE_PATTERNS = (
    r"/tag/",
    r"/tags/",
    r"/autor(?:es)?/",
    r"/author/",
    r"/video(?:s)?/",
    r"/videos?/",
    r"/galeria(?:s)?/",
    r"/album/",
    r"/newsletter",
    r"/podcast",
    r"/rss/",
    r"/hemeroteca",
    r"/archivo",
    r"/search",
    r"/buscar",
    r"/seccion/",
)
LIVEBLOG_PATTERNS = (r"/directo/", r"/live/", r"/envivo/", r"/en-vivo/", r"/ultima-hora")
OPINION_PATTERNS = (r"/opinion/", r"/tribuna/", r"/editorial/")
SPORTS_PATTERNS = (r"/deportes/", r"/futbol/", r"/baloncesto/", r"/tenis/", r"/motor/")


@dataclass(frozen=True)
class URLFilterResult:
    included: bool
    reason: str


def should_include_article_url(url: str, media: MediaConfig) -> URLFilterResult:
    path = urlsplit(url).path.lower()
    full = url.lower()

    if media.include_url_patterns and not _matches_any(full, media.include_url_patterns):
        return URLFilterResult(False, "not_matching_include_patterns")
    if _matches_any(full, tuple(DEFAULT_EXCLUDE_PATTERNS) + tuple(media.exclude_url_patterns)):
        return URLFilterResult(False, "excluded_pattern")
    if not media.include_liveblogs and _matches_any(full, LIVEBLOG_PATTERNS):
        return URLFilterResult(False, "liveblog")
    if not media.include_opinion and _matches_any(full, OPINION_PATTERNS):
        return URLFilterResult(False, "opinion")
    if not media.include_sports and _matches_any(full, SPORTS_PATTERNS):
        return URLFilterResult(False, "sports")
    clean_path = path.strip("/")
    if not clean_path or "/" not in clean_path:
        return URLFilterResult(False, "section_or_home")
    return URLFilterResult(True, "included")


def _matches_any(value: str, patterns: tuple[str, ...] | list[str]) -> bool:
    return any(re.search(pattern, value, flags=re.IGNORECASE) for pattern in patterns)
