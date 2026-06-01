from datetime import date

from src.audit import _count_entries
from src.discover import SitemapEntry
from src.models import MediaConfig


def test_audit_count_entries_tracks_date_and_url_filters() -> None:
    media = MediaConfig(name="Medio", domain="example.com")
    entries = [
        SitemapEntry("https://example.com/2026/05/31/noticia.html", lastmod="2026-05-31"),
        SitemapEntry("https://example.com/2026/05/31/videos/noticia.html", lastmod="2026-05-31"),
        SitemapEntry("https://example.com/2026/05/30/otra.html", lastmod="2026-05-30"),
    ]
    assert _count_entries(media, entries, date(2026, 5, 31)) == (3, 2, 1)
