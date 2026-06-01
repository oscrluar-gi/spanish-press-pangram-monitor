from src import database
from src.models import DiscoveredURL, ExtractedArticle


def test_database_deduplicates_discovered_url() -> None:
    conn = database.connect(":memory:")
    database.initialize_database(conn)
    item = DiscoveredURL(
        media_name="Medio",
        domain="example.com",
        url="https://example.com/story?utm_source=x",
        discovered_from="sitemap.xml",
        discovered_lastmod="2026-05-31",
        target_date="2026-05-31",
    )
    assert database.save_discovered_url(conn, item) is True
    assert database.save_discovered_url(conn, item) is False


def test_database_reuses_pangram_hash() -> None:
    conn = database.connect(":memory:")
    database.initialize_database(conn)
    item = DiscoveredURL(
        media_name="Medio",
        domain="example.com",
        url="https://example.com/story",
        discovered_from="sitemap.xml",
        target_date="2026-05-31",
    )
    database.save_discovered_url(conn, item)
    row = database.discovered_for_date(conn, "2026-05-31")[0]
    article = ExtractedArticle(
        url=row["url"],
        normalized_url=row["normalized_url"],
        target_date="2026-05-31",
        title="Title",
        author=None,
        article_published_at=None,
        article_modified_at=None,
        section=None,
        tags=[],
        canonical_url=None,
        language="es",
        is_paywalled=False,
        text_clean="texto limpio",
        word_count=2,
        text_hash="abc123",
        extraction_status="ok",
    )
    database.save_article(conn, row["id"], row["media_id"], row["media_name"], row["domain"], article)
    article_row = database.articles_ready_for_analysis(conn, "2026-05-31")[0]
    database.save_pangram_result(conn, article_row["id"], "abc123", {"prediction": "human"}, "ok")
    assert database.pangram_result_for_hash(conn, "abc123") is not None
    assert database.pangram_status_counts_for_date(conn, "2026-05-31") == {"ok": 1}


def test_has_no_ingested_data_detects_empty_date() -> None:
    conn = database.connect(":memory:")
    database.initialize_database(conn)

    assert database.has_no_ingested_data(conn, "2026-05-31") is True

    item = DiscoveredURL(
        media_name="Medio",
        domain="example.com",
        url="https://example.com/story",
        discovered_from="sitemap.xml",
        target_date="2026-05-31",
    )
    database.save_discovered_url(conn, item)

    assert database.has_no_ingested_data(conn, "2026-05-31") is False


def test_table_counts_and_latest_run_log_support_status_command() -> None:
    conn = database.connect(":memory:")
    database.initialize_database(conn)
    item = DiscoveredURL(
        media_name="Medio",
        domain="example.com",
        url="https://example.com/story",
        discovered_from="sitemap.xml",
        target_date="2026-05-31",
    )
    database.save_discovered_url(conn, item)
    row = database.discovered_for_date(conn, "2026-05-31")[0]
    conn.execute(
        """
        INSERT INTO wayback_snapshots
            (normalized_url, original_url, timestamp, statuscode, mimetype, digest, source_api)
        VALUES (?, ?, '20260531120000', '200', 'text/html', 'digest-1', 'cdx')
        """,
        (row["normalized_url"], row["url"]),
    )
    conn.commit()
    database.log_run(conn, "discover", "2026-05-31", "ok", "1 new URL")

    counts = database.table_counts(conn, "2026-05-31")
    assert counts["media"] == 1
    assert counts["discovered_urls"] == 1
    assert counts["articles"] == 0
    assert counts["pangram_results"] == 0
    assert counts["wayback_snapshots"] == 1
    assert database.latest_run_log(conn, limit=1)[0]["run_type"] == "discover"
