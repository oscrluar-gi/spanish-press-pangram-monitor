from datetime import date

from src.discover import SitemapEntry, entry_matches_target
from src.utils import parse_time_window, url_month_hint


def test_parse_time_window_defaults_to_hours_from_start() -> None:
    start, end = parse_time_window("2026-05-01", start_time="00:00", hours=4)
    assert start.isoformat() == "2026-05-01T00:00:00+02:00"
    assert end.isoformat() == "2026-05-01T04:00:00+02:00"


def test_entry_matches_target_time_window_uses_publication_datetime() -> None:
    window = parse_time_window("2026-05-01", start_time="00:00", hours=4)
    inside = SitemapEntry("https://example.com/story", publication_date="2026-05-01T02:30:00+02:00")
    outside = SitemapEntry("https://example.com/story-2", publication_date="2026-05-01T06:30:00+02:00")
    assert entry_matches_target(inside, date(2026, 5, 1), time_window=window)
    assert not entry_matches_target(outside, date(2026, 5, 1), time_window=window)


def test_url_month_hint_handles_compact_month_marker() -> None:
    assert url_month_hint("https://as.com/futbol/story-f202605-n/") == (2026, 5)
