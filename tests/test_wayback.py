from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

from src import database
from src.extract import _maybe_extract_wayback, should_try_wayback
from src.models import ExtractedArticle, WaybackConfig
from src.wayback_client import (
    WaybackClient,
    WaybackSnapshot,
    build_wayback_raw_url,
    build_wayback_replay_urls,
    find_best_snapshot,
    parse_availability_response,
    parse_cdx_response,
    select_best_snapshot,
)


TARGET = datetime(2026, 5, 31, 12, 0, tzinfo=ZoneInfo("Europe/Madrid"))


class FakeWaybackClient:
    _client = None
    headers = {"User-Agent": "test"}
    timeout_seconds = 10


class FakePartialFailureWaybackClient:
    def find_snapshots_cdx(self, *args, **kwargs):
        raise RuntimeError("cdx unavailable")

    def find_snapshot_availability(self, *args, **kwargs):
        return WaybackSnapshot("20260531100000", "https://example.com/story", "text/html", "200", source_api="availability")


class FakeExtendedSearchWaybackClient:
    def __init__(self) -> None:
        self.calls: list[tuple[int, int]] = []

    def find_snapshots_cdx(self, url, target_datetime, max_days_before, max_days_after):
        self.calls.append((max_days_before, max_days_after))
        if max_days_after <= 7:
            return []
        return [WaybackSnapshot("20260615100000", url, "text/html", "200", "digest", source_api="cdx")]

    def find_snapshot_availability(self, *args, **kwargs):
        return None


def test_parse_cdx_response_orders_by_closeness() -> None:
    payload = [
        ["timestamp", "original", "mimetype", "statuscode", "digest"],
        ["20260531100000", "https://example.com/story", "text/html", "200", "a"],
        ["20260603100000", "https://example.com/story", "text/html", "200", "b"],
    ]
    snapshots = parse_cdx_response(payload, TARGET)
    assert snapshots[0].timestamp == "20260531100000"
    assert snapshots[0].source_api == "cdx"
    assert snapshots[0].distance_seconds is not None


def test_find_domain_snapshots_cdx_uses_domain_match_type() -> None:
    seen_params = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_params.update(dict(request.url.params))
        return httpx.Response(
            200,
            json=[
                ["timestamp", "original", "mimetype", "statuscode", "digest"],
                ["20260501100000", "https://www.abc.es/espana/noticia.html", "text/html", "200", "digest"],
            ],
        )

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as http_client:
        client = WaybackClient(client=http_client, request_delay_seconds=0)
        snapshots = client.find_domain_snapshots_cdx(
            "abc.es",
            datetime(2026, 5, 1, 0, 0, tzinfo=ZoneInfo("Europe/Madrid")),
            datetime(2026, 5, 2, 0, 0, tzinfo=ZoneInfo("Europe/Madrid")),
            limit=25,
        )

    assert seen_params["url"] == "abc.es"
    assert seen_params["matchType"] == "domain"
    assert seen_params["collapse"] == "urlkey"
    assert seen_params["limit"] == "25"
    assert snapshots[0].original_url == "https://www.abc.es/espana/noticia.html"


def test_find_url_pattern_snapshots_cdx_keeps_query_narrow() -> None:
    seen_params = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_params.update(dict(request.url.params))
        return httpx.Response(
            200,
            json=[
                ["timestamp", "original", "mimetype", "statuscode", "digest"],
                ["20260501100000", "https://www.marca.com/futbol/2026/05/01/story.html", "text/html", "200", "digest"],
            ],
        )

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as http_client:
        client = WaybackClient(client=http_client, request_delay_seconds=0)
        snapshots = client.find_url_pattern_snapshots_cdx(
            "*.marca.com/*/2026/05/01/*",
            datetime(2026, 5, 1, 0, 0, tzinfo=ZoneInfo("Europe/Madrid")),
            datetime(2026, 5, 2, 0, 0, tzinfo=ZoneInfo("Europe/Madrid")),
            limit=10,
        )

    assert seen_params["url"] == "*.marca.com/*/2026/05/01/*"
    assert "matchType" not in seen_params
    assert snapshots[0].original_url == "https://www.marca.com/futbol/2026/05/01/story.html"


def test_parse_availability_response_accepts_only_available_200() -> None:
    payload = {
        "archived_snapshots": {
            "closest": {
                "available": True,
                "status": "200",
                "timestamp": "20260531100000",
                "url": "https://web.archive.org/web/20260531100000/https://example.com/story",
            }
        }
    }
    snapshot = parse_availability_response(payload, "https://example.com/story", TARGET)
    assert snapshot is not None
    assert snapshot.source_api == "availability"
    assert snapshot.statuscode == "200"
    assert parse_availability_response({"archived_snapshots": {"closest": {"available": True, "status": "404"}}}, "u", TARGET) is None


def test_select_best_snapshot_prefers_after_on_tie_and_rejects_window() -> None:
    before = WaybackSnapshot("20260531080000", "https://example.com/story", "text/html", "200")
    after = WaybackSnapshot("20260531120000", "https://example.com/story", "text/html", "200")
    far = WaybackSnapshot("20260630120000", "https://example.com/story", "text/html", "200")
    assert select_best_snapshot([before, after], TARGET).timestamp == "20260531120000"
    assert select_best_snapshot([far], TARGET, max_days_before=1, max_days_after=7) is None


def test_build_wayback_raw_url() -> None:
    snapshot = WaybackSnapshot("20260531100000", "https://example.com/story", "text/html", "200")
    assert build_wayback_raw_url(snapshot) == "https://web.archive.org/web/20260531100000id_/https://example.com/story"


def test_build_wayback_replay_urls_includes_fallback_modes() -> None:
    snapshot = WaybackSnapshot("20260531100000", "https://example.com/story", "text/html", "200")
    assert build_wayback_replay_urls(snapshot) == [
        "https://web.archive.org/web/20260531100000id_/https://example.com/story",
        "https://web.archive.org/web/20260531100000if_/https://example.com/story",
        "https://web.archive.org/web/20260531100000/https://example.com/story",
    ]


def test_find_best_snapshot_falls_back_to_availability_when_cdx_fails() -> None:
    snapshot = find_best_snapshot(
        FakePartialFailureWaybackClient(),
        "https://example.com/story",
        TARGET,
        WaybackConfig(use_cdx=True, use_availability=True),
    )
    assert snapshot is not None
    assert snapshot.source_api == "availability"


def test_find_best_snapshot_uses_extended_window_when_normal_window_is_empty() -> None:
    client = FakeExtendedSearchWaybackClient()
    snapshot = find_best_snapshot(
        client,
        "https://example.com/story",
        TARGET,
        WaybackConfig(
            max_days_before=1,
            max_days_after=7,
            extended_search=True,
            extended_max_days_before=7,
            extended_max_days_after=30,
            use_availability=False,
        ),
    )
    assert snapshot is not None
    assert snapshot.timestamp == "20260615100000"
    assert client.calls == [(1, 7), (7, 30)]


def test_fallback_only_uses_wayback_when_live_is_incomplete(monkeypatch) -> None:
    live = ExtractedArticle(
        url="https://example.com/story",
        normalized_url="https://example.com/story",
        target_date="2026-05-31",
        title=None,
        author=None,
        article_published_at=None,
        article_modified_at=None,
        section=None,
        tags=[],
        canonical_url=None,
        language=None,
        is_paywalled=False,
        text_clean=None,
        word_count=0,
        text_hash=None,
        extraction_status="no_text",
    )
    snapshot = WaybackSnapshot("20260531100000", "https://example.com/story", "text/html", "200", "digest", distance_seconds=3600)
    paragraph = " ".join([f"palabra{i}" for i in range(180)])
    html = f"<html><body><article><h1>Titulo</h1><p>{paragraph}</p></article></body></html>"
    monkeypatch.setattr("src.extract.find_best_snapshot", lambda *args, **kwargs: snapshot)
    monkeypatch.setattr("src.extract.fetch_html", lambda *args, **kwargs: html)

    article = _maybe_extract_wayback(
        {"url": "https://example.com/story", "target_date": "2026-05-31"},
        live,
        150,
        "fallback",
        WaybackConfig(),
        FakeWaybackClient(),
    )
    assert article.extraction_status == "ok_wayback"
    assert article.content_source == "wayback"
    assert article.source_url == "https://web.archive.org/web/20260531100000id_/https://example.com/story"


def test_wayback_replay_tries_alternate_modes_when_id_fails(monkeypatch) -> None:
    live = ExtractedArticle(
        url="https://example.com/story",
        normalized_url="https://example.com/story",
        target_date="2026-05-31",
        title=None,
        author=None,
        article_published_at=None,
        article_modified_at=None,
        section=None,
        tags=[],
        canonical_url=None,
        language=None,
        is_paywalled=False,
        text_clean=None,
        word_count=0,
        text_hash=None,
        extraction_status="no_text",
    )
    snapshot = WaybackSnapshot("20260531100000", "https://example.com/story", "text/html", "200", "digest", distance_seconds=3600)
    paragraph = " ".join([f"palabra{i}" for i in range(180)])
    html = f"<html><body><article><h1>Titulo</h1><p>{paragraph}</p></article></body></html>"
    seen_urls: list[str] = []

    def fake_fetch_html(client, url, retries=2):
        seen_urls.append(url)
        if "id_/" in url:
            raise httpx.ConnectError("connection refused")
        return html

    monkeypatch.setattr("src.extract.find_best_snapshot", lambda *args, **kwargs: snapshot)
    monkeypatch.setattr("src.extract.fetch_html", fake_fetch_html)

    article = _maybe_extract_wayback(
        {"url": "https://example.com/story", "target_date": "2026-05-31"},
        live,
        150,
        "fallback",
        WaybackConfig(),
        FakeWaybackClient(),
    )
    assert article.extraction_status == "ok_wayback"
    assert seen_urls == [
        "https://web.archive.org/web/20260531100000id_/https://example.com/story",
        "https://web.archive.org/web/20260531100000if_/https://example.com/story",
    ]
    assert article.source_url == "https://web.archive.org/web/20260531100000if_/https://example.com/story"


def test_fallback_does_not_use_wayback_when_live_is_ok(monkeypatch) -> None:
    live = ExtractedArticle(
        url="https://example.com/story",
        normalized_url="https://example.com/story",
        target_date="2026-05-31",
        title="Ok",
        author=None,
        article_published_at=None,
        article_modified_at=None,
        section=None,
        tags=[],
        canonical_url=None,
        language=None,
        is_paywalled=False,
        text_clean="texto suficiente",
        word_count=200,
        text_hash="hash",
        extraction_status="ok_live",
    )
    monkeypatch.setattr("src.extract.find_best_snapshot", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not call Wayback")))
    article = _maybe_extract_wayback(
        {"url": "https://example.com/story", "target_date": "2026-05-31"},
        live,
        150,
        "fallback",
        WaybackConfig(),
        FakeWaybackClient(),
    )
    assert article is live


def test_should_try_wayback_detects_httpx_status_messages() -> None:
    article = ExtractedArticle(
        url="https://example.com/story",
        normalized_url="https://example.com/story",
        target_date="2026-05-01",
        title=None,
        author=None,
        article_published_at=None,
        article_modified_at=None,
        section=None,
        tags=[],
        canonical_url=None,
        language=None,
        is_paywalled=False,
        text_clean=None,
        word_count=0,
        text_hash=None,
        extraction_status="http_error",
        error="Client error '403 Forbidden' for url 'https://example.com/story'",
    )
    assert should_try_wayback(article) is True


def test_database_does_not_duplicate_snapshots() -> None:
    conn = database.connect(":memory:")
    database.initialize_database(conn)
    snapshot = WaybackSnapshot("20260531100000", "https://example.com/story", "text/html", "200", "digest")
    database.save_wayback_snapshot(conn, "https://example.com/story", snapshot, selected=True)
    database.save_wayback_snapshot(conn, "https://example.com/story", snapshot, selected=True)
    count = conn.execute("SELECT COUNT(*) AS total FROM wayback_snapshots").fetchone()["total"]
    assert count == 1


def test_wayback_incomplete_not_ready_for_pangram() -> None:
    conn = database.connect(":memory:")
    database.initialize_database(conn)
    conn.execute("INSERT INTO media (id, name, domain, rss_feeds_json) VALUES (1, 'Medio', 'example.com', '[]')")
    conn.execute(
        """
        INSERT INTO discovered_urls
            (id, media_id, media_name, domain, url, normalized_url, discovered_from, target_date)
        VALUES (1, 1, 'Medio', 'example.com', 'https://example.com/story',
                'https://example.com/story', 'test', '2026-05-31')
        """
    )
    conn.execute(
        """
        INSERT INTO articles
            (discovered_url_id, media_id, media_name, domain, url, normalized_url, target_date,
             text_clean, word_count, text_hash, extraction_status, content_source)
        VALUES (1, 1, 'Medio', 'example.com', 'https://example.com/story',
                'https://example.com/story', '2026-05-31', 'texto', 1, 'hash', 'wayback_too_short', 'wayback')
        """
    )
    assert database.articles_ready_for_analysis(conn, "2026-05-31") == []
