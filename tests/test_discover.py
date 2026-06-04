import inspect
from datetime import date

from src.discover import (
    SitemapEntry,
    discover_media_gdelt,
    discover_all,
    discover_media_wayback_rss,
    discover_media_wayback_cdx,
    entry_matches_target,
    expand_sitemap_urls,
    load_media_config,
    match_discovery_keywords,
    media_entry_matches_target,
    parse_rss_xml,
    parse_sitemap_xml,
    prioritize_sitemap_index_entries,
    sitemap_candidates_for_media,
    gdelt_threshold_for_media,
    should_use_gdelt_discovery,
    should_try_gdelt_discovery,
    should_try_wayback_discovery,
    should_try_wayback_rss_discovery,
    should_use_wayback_discovery,
    wayback_threshold_for_media,
    _fallback_media_order,
    wayback_discovery_patterns,
)
from src.gdelt_client import GdeltArticle
from src.models import DiscoveredURL, GdeltConfig, MediaConfig, WaybackConfig
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


def test_match_discovery_keywords_is_accent_insensitive() -> None:
    entry = SitemapEntry(
        loc="https://example.com/politica/pedro-sanchez-psoe.html",
        title="Pedro S\u00e1nchez se reune con el PSOE",
        metadata={"keywords": "Gobierno, Moncloa"},
    )

    assert match_discovery_keywords(entry, ["Pedro Sanchez"]) == ["Pedro Sanchez"]
    assert match_discovery_keywords(entry, ["Sanchez", "PSOE"], mode="all") == ["Sanchez", "PSOE"]


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


def test_sitemap_candidates_use_canonical_domain_and_adapter_urls() -> None:
    media = MediaConfig("ABC", "abc.es", canonical_domain="www.abc.es", adapter="abc")
    candidates = sitemap_candidates_for_media(media, date(2026, 5, 31), robots_sitemaps=[])

    assert ("https://www.abc.es/sitemap-index.xml", "adapter:abc", "adapter_sitemap") in candidates
    assert any(item[0] == "https://www.abc.es/sitemap.xml" for item in candidates)
    assert all("https://abc.es/sitemap" not in item[0] for item in candidates)


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


def test_as_adapter_uses_lastmod_instead_of_accepting_whole_month() -> None:
    media = MediaConfig("AS", "as.com", adapter="as", allow_month_fallback=True)
    may_url = "https://as.com/futbol/noticia-f202605-n/"

    assert media_entry_matches_target(
        media,
        SitemapEntry(may_url, lastmod="2026-05-08T10:00:00+02:00"),
        date(2026, 5, 8),
    )
    assert not media_entry_matches_target(
        media,
        SitemapEntry(may_url, lastmod="2026-05-09T10:00:00+02:00"),
        date(2026, 5, 8),
    )


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


class FakeWaybackRssClient:
    def find_resource_snapshots_cdx(self, url, start, end, *, limit, mimetype=None):
        assert url == "https://www.elmundo.es/rss/portada.xml"
        return [
            WaybackSnapshot("20260508120000", url, "application/rss+xml", "200", "rssdigest"),
        ]

    def fetch_text(self, url):
        assert "20260508120000id_/https://www.elmundo.es/rss/portada.xml" in url
        return """<rss><channel>
        <item>
          <title>Noticia politica</title>
          <link>https://www.elmundo.es/espana/2026/05/08/story.html</link>
          <pubDate>Fri, 08 May 2026 12:00:00 +0200</pubDate>
        </item>
        <item>
          <title>Video</title>
          <link>https://www.elmundo.es/videos/2026/05/08/video.html</link>
          <pubDate>Fri, 08 May 2026 12:00:00 +0200</pubDate>
        </item>
        </channel></rss>"""


def test_wayback_rss_discovery_parses_archived_feed() -> None:
    media = MediaConfig(
        "El Mundo",
        "elmundo.es",
        canonical_domain="www.elmundo.es",
        adapter="elmundo",
        rss_feeds=["https://www.elmundo.es/rss/portada.xml"],
    )
    rows = discover_media_wayback_rss(media, date(2026, 5, 8), FakeWaybackRssClient(), WaybackConfig())

    assert len(rows) == 1
    assert rows[0].url == "https://www.elmundo.es/espana/2026/05/08/story.html"
    assert rows[0].source_type == "wayback_rss"
    assert rows[0].discovery_metadata["wayback_rss_timestamp"] == "20260508120000"


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


def test_should_try_wayback_rss_discovery_requires_adapter_opt_in() -> None:
    config = WaybackConfig(discovery_min_candidates=2)
    media = MediaConfig("El Mundo", "elmundo.es", adapter="elmundo", rss_feeds=["https://www.elmundo.es/rss/portada.xml"])

    assert should_try_wayback_rss_discovery(media, config, 0, date(2026, 5, 8))
    assert not should_try_wayback_rss_discovery(media, config, 2, date(2026, 5, 8))
    assert not should_try_wayback_rss_discovery(MediaConfig("ABC", "abc.es", adapter="abc"), config, 0, date(2026, 5, 8))


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


class FakeCanonicalGdeltClient:
    def __init__(self) -> None:
        self.domain = None

    def find_articles(self, domain, start, end, *, limit):
        self.domain = domain
        return [
            GdeltArticle("https://abc.es/espana/noticia.html", "Titulo", "20260501T210000Z"),
        ]


def test_gdelt_discovery_uses_configured_gdelt_domain_and_adapter() -> None:
    media = MediaConfig(
        "ABC",
        "abc.es",
        canonical_domain="www.abc.es",
        gdelt_domain="abc.es",
        adapter="abc",
        gdelt_discovery=True,
        gdelt_discovery_limit=25,
    )
    client = FakeCanonicalGdeltClient()
    rows = discover_media_gdelt(media, date(2026, 5, 1), client, default_limit=50)

    assert client.domain == "abc.es"
    assert rows[0].url == "https://www.abc.es/espana/noticia.html"
    assert rows[0].discovery_metadata["source_trace"]["adapter"] == "abc"


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


def test_fallback_threshold_helpers_use_media_override_or_global_default() -> None:
    media = MediaConfig("ABC", "abc.es")
    custom = MediaConfig("ABC", "abc.es", gdelt_discovery_min_candidates=12, wayback_discovery_min_candidates=3)

    assert gdelt_threshold_for_media(media, GdeltConfig(discovery_min_candidates=25)) == 25
    assert gdelt_threshold_for_media(custom, GdeltConfig(discovery_min_candidates=25)) == 12
    assert wayback_threshold_for_media(media, WaybackConfig(discovery_min_candidates=2)) == 2
    assert wayback_threshold_for_media(custom, WaybackConfig(discovery_min_candidates=2)) == 3


def test_fallback_media_order_prioritizes_low_candidate_media() -> None:
    low = MediaConfig("El Mundo", "elmundo.es", gdelt_discovery=True)
    medium = MediaConfig("Cinco Dias", "cincodias.elpais.com", gdelt_discovery=True, gdelt_discovery_min_candidates=50)
    enough = MediaConfig("AS", "as.com", gdelt_discovery=True)
    rows = {
        "El Mundo": [],
        "Cinco Dias": [
            DiscoveredURL("Cinco Dias", "cincodias.elpais.com", f"https://example.com/{index}", "test", "2026-05-15")
            for index in range(34)
        ],
        "AS": [
            DiscoveredURL("AS", "as.com", f"https://as.com/{index}", "test", "2026-05-15")
            for index in range(300)
        ],
    }

    ordered = _fallback_media_order(
        [enough, medium, low],
        rows,
        "gdelt",
        GdeltConfig(discovery_min_candidates=25),
        WaybackConfig(),
        date(2026, 5, 15),
    )

    assert [media.name for media in ordered] == ["El Mundo", "Cinco Dias"]


def test_wayback_discovery_patterns_can_be_configured() -> None:
    media = MediaConfig(
        "ABC",
        "abc.es",
        wayback_discovery_patterns=["*.{domain}/*/{year}/{month}/{day}/*"],
    )
    assert wayback_discovery_patterns(media, date(2026, 5, 1)) == [
        "*.abc.es/*/2026/05/01/*",
    ]


def test_wayback_discovery_patterns_use_configured_domains() -> None:
    media = MediaConfig("ABC", "abc.es", wayback_domains=["abc.es", "www.abc.es"])

    patterns = wayback_discovery_patterns(media, date(2026, 5, 1))

    assert "abc.es/*/2026/05/01/*" in patterns
    assert "www.abc.es/*/2026/05/01/*" in patterns
    assert all("www.www." not in pattern for pattern in patterns)


def test_load_media_config_reads_abc_domain_strategy() -> None:
    abc = next(media for media in load_media_config("config/media.yaml") if media.name == "ABC")

    assert abc.domain == "abc.es"
    assert abc.canonical_domain == "www.abc.es"
    assert abc.gdelt_domain == "abc.es"
    assert abc.wayback_domains == ["abc.es", "www.abc.es"]
    assert abc.adapter == "abc"


def test_config_assigns_adapters_to_all_media() -> None:
    media_items = load_media_config("config/media.yaml")

    assert all(media.adapter for media in media_items)
