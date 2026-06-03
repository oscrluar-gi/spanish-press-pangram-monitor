from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

from src.gdelt_client import GdeltClient, format_gdelt_datetime, load_gdelt_config, parse_gdelt_articles, url_matches_domain
from src.models import GdeltConfig


def test_parse_gdelt_articles_extracts_expected_fields() -> None:
    payload = {
        "articles": [
            {
                "url": "https://www.abc.es/espana/story.html",
                "title": "Title",
                "seendate": "20260501T210000Z",
                "sourceCountry": "Spain",
            }
        ]
    }
    articles = parse_gdelt_articles(payload)
    assert articles[0].url == "https://www.abc.es/espana/story.html"
    assert articles[0].title == "Title"
    assert articles[0].seendate == "20260501T210000Z"
    assert articles[0].source_country == "Spain"


def test_format_gdelt_datetime_converts_madrid_to_utc() -> None:
    value = datetime(2026, 5, 1, 5, 0, tzinfo=ZoneInfo("Europe/Madrid"))
    assert format_gdelt_datetime(value) == "20260501030000"


def test_url_matches_domain_accepts_subdomains() -> None:
    assert url_matches_domain("https://www.marca.com/futbol/story.html", "marca.com")
    assert url_matches_domain("https://marca.com/futbol/story.html", "marca.com")
    assert not url_matches_domain("https://example.com/story.html", "marca.com")


def test_gdelt_client_retries_429_and_filters_domain() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429)
        return httpx.Response(
            200,
            json={
                "articles": [
                    {"url": "https://www.abc.es/espana/story.html", "title": "ABC", "seendate": "20260501T210000Z"},
                    {"url": "https://other.example/story.html", "title": "Other"},
                ]
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        client = GdeltClient(
            client=http_client,
            config=GdeltConfig(request_delay_seconds=0, max_retries=1, retry_delay_seconds=0, retry_jitter_seconds=0, cooldown_seconds=0),
        )
        rows = client.find_articles(
            "abc.es",
            datetime(2026, 5, 1, 0, 0, tzinfo=ZoneInfo("Europe/Madrid")),
            datetime(2026, 5, 2, 0, 0, tzinfo=ZoneInfo("Europe/Madrid")),
            limit=10,
        )

    assert calls == 2
    assert len(rows) == 1
    assert rows[0].url == "https://www.abc.es/espana/story.html"


def test_gdelt_client_caches_successful_requests() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={"articles": [{"url": "https://www.abc.es/espana/story.html", "title": "ABC"}]},
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        client = GdeltClient(
            client=http_client,
            config=GdeltConfig(request_delay_seconds=0, retry_jitter_seconds=0, cooldown_seconds=0),
        )
        start = datetime(2026, 5, 1, 0, 0, tzinfo=ZoneInfo("Europe/Madrid"))
        end = datetime(2026, 5, 2, 0, 0, tzinfo=ZoneInfo("Europe/Madrid"))

        assert len(client.find_articles("abc.es", start, end, limit=10)) == 1
        assert len(client.find_articles("abc.es", start, end, limit=10)) == 1

    assert calls == 1


def test_gdelt_client_activates_global_cooldown_after_429() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429)

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        client = GdeltClient(
            client=http_client,
            config=GdeltConfig(request_delay_seconds=0, max_retries=1, retry_delay_seconds=0, retry_jitter_seconds=0, cooldown_seconds=300),
        )
        start = datetime(2026, 5, 1, 0, 0, tzinfo=ZoneInfo("Europe/Madrid"))
        end = datetime(2026, 5, 2, 0, 0, tzinfo=ZoneInfo("Europe/Madrid"))

        assert client.find_articles("abc.es", start, end, limit=10) == []
        assert client.find_articles("elmundo.es", start, end, limit=10) == []

    assert calls == 1


def test_load_gdelt_config_defaults_to_lower_maxrecords(tmp_path, monkeypatch) -> None:
    config_path = tmp_path / "media.yaml"
    config_path.write_text("media: []\n", encoding="utf-8")
    monkeypatch.delenv("GDELT_MAX_RESULTS_PER_MEDIA", raising=False)

    config = load_gdelt_config(str(config_path))

    assert config.max_results_per_media == 25
    assert config.discovery_min_candidates == 5
