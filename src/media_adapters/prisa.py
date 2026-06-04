from __future__ import annotations

from datetime import date, datetime
from urllib.parse import urlsplit

from src.media_adapters.base import MediaAdapter
from src.models import MediaConfig
from src.utils import datetime_in_window, datetime_matches_target, parse_datetime, url_date_hint

if False:
    from src.discover import SitemapEntry


class PrisaAdapter(MediaAdapter):
    name = "prisa"


class AsAdapter(PrisaAdapter):
    name = "as"

    def entry_matches_target(
        self,
        entry: "SitemapEntry",
        target: date,
        time_window: tuple[datetime, datetime] | None,
        allow_month_fallback: bool,
    ) -> bool | None:
        if time_window is not None:
            start, end = time_window
            return bool(entry.lastmod and datetime_in_window(entry.lastmod, start, end))
        hinted = url_date_hint(entry.loc)
        if hinted is not None:
            return hinted == target
        if parse_datetime(entry.lastmod):
            return datetime_matches_target(entry.lastmod, target)
        return None

    def should_include_url(self, url: str) -> bool:
        path = urlsplit(url).path.lower()
        if path.endswith("-v/") or "/videos/" in path:
            return False
        return True


class CincoDiasAdapter(PrisaAdapter):
    name = "cincodias"

    def should_include_url(self, url: str) -> bool:
        path = urlsplit(url).path.lower()
        if "/smartlife/" in path:
            return False
        return True
