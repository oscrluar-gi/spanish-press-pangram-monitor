import inspect
from datetime import date

from src.discover import (
    SitemapEntry,
    discover_media_gdelt,
    discover_all,
    discover_media_wayback_cdx,
    entry_matches_target,
    expand_sitemap_urls,
    match_discovery_keywords,
    parse_rss_xml,
    parse_sitemap_xml,
    prioritize_sitemap_index_entries,
    should_use_gdelt_discovery,
    should_try_gdelt_discovery,
    should_try_wayback_discovery,
    should_use_wayback_discovery,
    wayback_discovery_patterns,
)
from src.gdelt_client import GdeltArticle
from src.models import GdeltConfig, MediaConfig, WaybackConfig
from src.wayback_client import WaybackSnapshot


def test_parse_urlset_sitemap_with_news_date() -> None:
    xml = b"""<?xml version="1.0" encoding="UTF-8"?>
    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"
            xmlns:news="http://www.google.com/schemas/sitemap-news/0.9">
      <url>
        <loc>https://example.com/2026/05/31/article.html</loc>
        <lastmod>2026-05-31T08:00:00+02:00</lastmod>
        <news:news>
          <news:publication_date>2026-05-31T07:45:00+02:00</news:publication_date>
          <news:title>El Congreso aprueba una reforma de vivienda</news:title>
          <news:keywords>vivienda, congreso</news:keywords>
        </news:news>
      </url>
    </urlset>"""
    kind, entries = parse_sitemap_xml(xml)
    assert kind == "urlset"
    assert entries == [
        SitemapEntry(
            loc="https://example.com/2026/05/31/article.html",
            lastmod="2026-05-31T08:00:00+02:00",
            publication_date="2026-05-31T07:45:00+02:00",
            title="El Congreso aprueba una reforma de vivienda",
            metadata={
                "publication_date": "2026-05-31T07:45:00+02:00",
                "title": "El Congreso aprueba una reforma de vivienda",
                "keywords": "vivienda, congreso",
            },
        )
    ]
    assert entry_matches_target(entries[0], date(2026, 5, 31))


def test_discover_all_accepts_keyword_filters() -> None:
    signature = inspect.signature(discover_all)

    assert "keywords" in signature.parameters
    assert "keyword_mode" in signature.parameters


def test_parse_sitemap_index() -> None:
    xml = b"""<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <sitemap>
        <loc>https://example.com/sitemap-news.xml</loc>
        <lastmod>2026-05-31</lastmod>
      </sitemap>
    </sitemapindex>"""
    kind, entries = parse_sitemap_xml(xml)
    assert kind == "sitemapindex"
    assert entries[0].loc == "https://example.com/sitemap-news.xml"


def test_parse_rss_xml() -> None:
    xml = b"""<rss><channel><item>
      <link>https://example.com/story</link>
      <title>Sanidad anuncia nuevas plazas MIR</title>
      <category>sanidad</category>
      <pubDate>Sun, 31 May 2026 10:15:00 +0200</pubDate>
    </item></channel></rss>"""
    entries = parse_rss_xml(xml)
    assert entries[0].loc == "https://example.com/story"
    assert entries[0].title == "Sanidad anuncia nuevas plazas MIR"
    assert entries[0].metadata["categories"] == ["sanidad"]
    assert entry_matches_target(entries[0], date(2026, 5, 31))


def test_match_discovery_keywords_uses_url_title_and_metadata() -> None:
    entry = SitemapEntry(
        loc="https://example.com/economia/story.html",
        title="El alquiler sube en Madrid",
        metadata={"keywords": "vivienda, precios"},
    )

    assert match_discovery_keywords(entry, ["economia"]) == ["economia"]
    assert match_discovery_keywords(entry, ["alquiler"]) == ["alquiler"]
    assert match_discovery_keywords(entry, ["vivienda"]) == ["vivienda"]
    assert match_discovery_keywords(entry, ["alquiler", "vivienda"], mode="all") == ["alquiler", "vivienda"]
    assert match_discovery_keywords(entry, ["alquiler", "sanidad"], mode="all") == []


def test_expand_sitemap_url_templates() -> None:
    media = MediaConfig(
        name="Test",
        domain="example.com",
        sitemap_urls=["https://example.com/sitemaps/{year}/{month}/sitemap_{page}.xml"],
        sitemap_page_range=(0, 2),
    )
    assert expand_sitemap_urls(media, date(2026, 5, 31)) == [
        ("https://example.com/sitemaps/2026/05/sitemap_0.xml", "https://example.com/sitemaps/{year}/{month}/sitemap_{page}.xml"),
        ("https://example.com/sitemaps/2026/05/sitemap_1.xml", "https://example.com/sitemaps/{year}/{month}/sitemap_{page}.xml"),
        ("https://example.com/sitemaps/2026/05/sitemap_2.xml", "https://example.com/sitemaps/{year}/{month}/sitemap_{page}.xml"),
    ]


def test_prioritize_sitemap_index_entries_puts_target_month_first() -> None:
    entries = [
        SitemapEntry("https://example.com/sitemap-noticias-201103.xml.gz", lastmod="2024-12-10T00:00:00+01:00"),
        SitemapEntry("https://example.com/sitemap-google-news.xml", lastmod="2026-06-01T00:00:00+02:00"),
        SitemapEntry("https://example.com/sitemap-noticias-202605.xml.gz", lastmod="2026-05-02T00:00:00+02:00"),
    ]
    prioritized = prioritize_sitemap_index_entries(entries, date(2026, 5, 1))
    assert prioritized[0].loc == "https://example.com/sitemap-noticias-202605.xml.gz"


def test_entry_matches_target_with_month_fallback() -> None:
    entry = SitemapEntry(
        "https://as.com/futbol/mundial/rashford-impacta-en-casa-de-messi-f202605-n/",
        lastmod="2026-06-01T00:14:44+02:00",
    )
    assert not entry_matches_target(entry, date(2026, 5, 1))
    assert entry_matches_target(entry, date(2026, 5, 1), allow_month_fallback=True)


class FakeCdxDiscoveryClient:
    def __init__(self) -> None:
        self.patterns: list[str] = []

    def find_url_pattern_snapshots_cdx(self, pattern, start, end, *, limit):
        self.patterns.append(pattern)
        assert limit == 100
        if "/2026/05/01/" not in pattern:
            return []
        return [
            WaybackSnapshot("20260501100000", "https://www.abc.es/espana/noticia-uno.html", "text/html", "200", "a"),
            WaybackSnapshot("20260501110000", "https://www.abc.es/videos/noticia-video.html", "text/html", "200", "b"),
            WaybackSnapshot("20260502100000", "https://www.abc.es/espana/noticia-uno.html?utm_source=x", "text/html", "200", "c"),
            WaybackSnapshot("20260501120000", "https://www.abc.es/espana/2026/05/02/noticia-dos.html", "text/html", "200", "d"),
        ]


def test_wayback_cdx_discovery_filters_and_dedupes_articles() -> None:
    media = MediaConfig("ABC", "abc.es", wayback_discovery=True, wayback_discovery_limit=100)
    client = FakeCdxDiscoveryClient()
    rows = discover_media_wayback_cdx(
        media,
        date(2026, 5, 1),
        client,
        WaybackConfig(discovery_max_urls_per_media=50),
    )

    assert len(rows) == 1
    assert rows[0].url == "https://www.abc.es/espana/noticia-uno.html"
    assert rows[0].source_type == "wayback_cdx_discovery"
    assert rows[0].discovered_from == "wayback_cdx:20260501100000"
    assert client.patterns[0] == "www.abc.es/*/2026/05/01/*"


def test_should_use_wayback_discovery_requires_media_opt_in() -> None:
    assert should_use_wayback_discovery(MediaConfig("ABC", "abc.es", wayback_discovery=True), WaybackConfig())
    assert not should_use_wayback_discovery(MediaConfig("ABC", "abc.es"), WaybackConfig())
    assert not should_use_wayback_discovery(
        MediaConfig("ABC", "abc.es", wayback_discovery=True),
        WaybackConfig(discovery_enabled=False),
    )


def test_should_try_wayback_discovery_uses_candidate_threshold() -> None:
    media = MediaConfig("ABC", "abc.es", wayback_discovery=True)
    config = WaybackConfig(discovery_min_candidates=2)

    assert should_try_wayback_discovery(media, config, candidate_count=0)
    assert should_try_wayback_discovery(media, config, candidate_count=1)
    assert not should_try_wayback_discovery(media, config, candidate_count=2)

    custom_media = MediaConfig("ABC", "abc.es", wayback_discovery=True, wayback_discovery_min_candidates=5)
    assert should_try_wayback_discovery(custom_media, config, candidate_count=4)
    assert not should_try_wayback_discovery(custom_media, config, candidate_count=5)


class FakeGdeltClient:
    def find_articles(self, domain, start, end, *, limit):
        assert domain == "abc.es"
        assert limit == 100
        return [
            GdeltArticle("https://www.abc.es/espana/noticia.html", "Titulo", "20260501T210000Z"),
            GdeltArticle("https://www.abc.es/videos/noticia.html", "Video", "20260501T210000Z"),
            GdeltArticle("https://www.abc.es/espana/noticia.html?utm_source=x", "Duplicado", "20260501T210000Z"),
        ]


def test_gdelt_discovery_filters_and_dedupes_articles() -> None:
    media = MediaConfig("ABC", "abc.es", gdelt_discovery=True, gdelt_discovery_limit=100, discovery_keywords=["titulo"])
    rows = discover_media_gdelt(media, date(2026, 5, 1), FakeGdeltClient(), default_limit=50)

    assert len(rows) == 1
    assert rows[0].url == "https://www.abc.es/espana/noticia.html"
    assert rows[0].source_type == "gdelt"
    assert rows[0].discovered_from == "gdelt"
    assert rows[0].discovered_lastmod == "20260501T210000Z"
    assert rows[0].discovery_title == "Titulo"
    assert rows[0].matched_keywords == ["titulo"]


def test_should_use_gdelt_discovery_requires_media_opt_in() -> None:
    assert should_use_gdelt_discovery(MediaConfig("ABC", "abc.es"), True)
    assert not should_use_gdelt_discovery(MediaConfig("ABC", "abc.es", gdelt_discovery=False), True)
    assert not should_use_gdelt_discovery(MediaConfig("ABC", "abc.es", gdelt_discovery=True), False)


def test_should_try_gdelt_discovery_uses_candidate_threshold() -> None:
    media = MediaConfig("ABC", "abc.es", gdelt_discovery=True)
    config = GdeltConfig(discovery_min_candidates=5)

    assert should_try_gdelt_discovery(media, config, candidate_count=0)
    assert should_try_gdelt_discovery(media, config, candidate_count=4)
    assert not should_try_gdelt_discovery(media, config, candidate_count=5)

    custom_media = MediaConfig("ABC", "abc.es", gdelt_discovery=True, gdelt_discovery_min_candidates=2)
    assert should_try_gdelt_discovery(custom_media, config, candidate_count=1)
    assert not should_try_gdelt_discovery(custom_media, config, candidate_count=2)


def test_wayback_discovery_patterns_can_be_configured() -> None:
    media = MediaConfig(
        "ABC",
        "abc.es",
        wayback_discovery_patterns=["*.{domain}/*/{year}/{month}/{day}/*"],
    )
    assert wayback_discovery_patterns(media, date(2026, 5, 1)) == [
        "*.abc.es/*/2026/05/01/*",
    ]
