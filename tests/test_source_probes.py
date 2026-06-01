from datetime import date

import httpx

from src.models import MediaConfig
from src.source_probes import (
    cdx_snapshots,
    parse_cdx_json,
    parse_gdelt_articles,
    parse_robots_sitemaps,
    parse_search_result_links,
    probe_gdelt,
)


def test_parse_robots_sitemaps_extracts_multiple_lines() -> None:
    text = """
    User-agent: *
    Sitemap: https://example.com/sitemap-news.xml
    sitemap: https://example.com/sitemap-archive.xml
    """
    assert parse_robots_sitemaps(text) == [
        "https://example.com/sitemap-news.xml",
        "https://example.com/sitemap-archive.xml",
    ]


def test_parse_cdx_json_maps_header_rows() -> None:
    payload = [
        ["timestamp", "original", "mimetype", "statuscode", "digest"],
        ["20260501090000", "https://example.com/rss.xml", "text/xml", "200", "abc"],
    ]
    assert parse_cdx_json(payload) == [
        {
            "timestamp": "20260501090000",
            "original": "https://example.com/rss.xml",
            "mimetype": "text/xml",
            "statuscode": "200",
            "digest": "abc",
        }
    ]


def test_parse_search_result_links_unwraps_duckduckgo_urls() -> None:
    html = """
    <html><body>
      <a class="result__a" href="/l/?uddg=https%3A%2F%2Fwww.abc.es%2Fespana%2Fstory.html">ABC</a>
      <a href="https://duckduckgo.com/y.js">ignore</a>
    </body></html>
    """
    assert parse_search_result_links(html) == ["https://www.abc.es/espana/story.html"]


def test_parse_gdelt_articles_returns_article_dicts() -> None:
    payload = {"articles": [{"url": "https://example.com/story", "title": "Title", "seendate": "20260501T090000Z"}]}
    assert parse_gdelt_articles(payload) == [
        {"url": "https://example.com/story", "title": "Title", "seendate": "20260501T090000Z"}
    ]


def test_cdx_snapshots_uses_status_filter_without_mimetype_when_requested() -> None:
    seen_params = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_params.update(dict(request.url.params))
        return httpx.Response(
            200,
            json=[
                ["timestamp", "original", "mimetype", "statuscode", "digest"],
                ["20260501090000", "https://example.com/robots.txt", "text/plain", "200", "abc"],
            ],
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        snapshots, error = cdx_snapshots(
            client,
            "https://example.com/robots.txt",
            date(2026, 5, 1),
            max_results=5,
            mimetype_filter=None,
        )

    assert error is None
    assert snapshots[0]["original"] == "https://example.com/robots.txt"
    assert seen_params["filter"] == "statuscode:200"


def test_probe_gdelt_filters_to_media_domain() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "articles": [
                    {
                        "url": "https://www.abc.es/espana/story.html",
                        "title": "ABC story",
                        "seendate": "20260501T090000Z",
                    },
                    {"url": "https://other.example/story.html", "title": "Other"},
                ]
            },
        )

    media = MediaConfig("ABC", "abc.es")
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        rows = probe_gdelt(client, media, date(2026, 5, 1), max_results=10)

    assert len(rows) == 1
    assert rows[0].strategy == "gdelt"
    assert rows[0].candidate_url == "https://www.abc.es/espana/story.html"
