import csv

from src import database
from src.validation import export_validation, validate_rows


def _seed_validation_db():
    conn = database.connect(":memory:")
    database.initialize_database(conn)
    conn.execute("INSERT INTO media (id, name, domain, rss_feeds_json) VALUES (1, 'Medio A', 'a.example', '[]')")
    conn.execute("INSERT INTO media (id, name, domain, rss_feeds_json) VALUES (2, 'Medio B', 'b.example', '[]')")
    discovered = [
        (1, 1, "Medio A", "a.example", "https://a.example/2026/05/31/uno.html", "https://a.example/2026/05/31/uno.html"),
        (2, 1, "Medio A", "a.example", "https://a.example/videos/clip.html", "https://a.example/videos/clip.html"),
        (3, 1, "Medio A", "a.example", "https://a.example/2026/05/31/tres.html", "https://a.example/2026/05/31/tres.html"),
        (4, 2, "Medio B", "b.example", "https://b.example/2026/05/31/cuatro.html", "https://b.example/2026/05/31/cuatro.html"),
    ]
    conn.executemany(
        """
        INSERT INTO discovered_urls
            (id, media_id, media_name, domain, url, normalized_url, discovered_from, target_date)
        VALUES (?, ?, ?, ?, ?, ?, 'test', '2026-05-31')
        """,
        discovered,
    )
    long_text = " ".join(["palabra"] * 350)
    short_text = " ".join(["breve"] * 120)
    conn.execute(
        """
        INSERT INTO articles
            (id, discovered_url_id, media_id, media_name, domain, url, normalized_url, target_date,
             title, article_published_at, text_clean, word_count, text_hash, extraction_status, content_source)
        VALUES (1, 1, 1, 'Medio A', 'a.example', 'https://a.example/2026/05/31/uno.html',
                'https://a.example/2026/05/31/uno.html', '2026-05-31', 'Uno',
                '2026-05-31T10:00:00+02:00', ?, 350, 'hash-live', 'ok_live', 'live')
        """,
        (long_text,),
    )
    conn.execute(
        """
        INSERT INTO articles
            (id, discovered_url_id, media_id, media_name, domain, url, normalized_url, target_date,
             title, text_clean, word_count, text_hash, extraction_status, content_source,
             wayback_timestamp, wayback_distance_seconds)
        VALUES (2, 2, 1, 'Medio A', 'a.example', 'https://a.example/videos/clip.html',
                'https://a.example/videos/clip.html', '2026-05-31', '',
                ?, 120, 'hash-wayback', 'ok_wayback', 'wayback', '20260604100000', 80 * 3600)
        """,
        (short_text,),
    )
    conn.execute(
        """
        INSERT INTO articles
            (id, discovered_url_id, media_id, media_name, domain, url, normalized_url, target_date,
             title, word_count, extraction_status, content_source, skip_reason)
        VALUES (3, 3, 1, 'Medio A', 'a.example', 'https://a.example/2026/05/31/tres.html',
                'https://a.example/2026/05/31/tres.html', '2026-05-31', 'Tres', 0, 'no_text', 'live', 'no text')
        """
    )
    conn.execute(
        """
        INSERT INTO articles
            (id, discovered_url_id, media_id, media_name, domain, url, normalized_url, target_date,
             title, text_clean, word_count, text_hash, extraction_status, content_source)
        VALUES (4, 4, 2, 'Medio B', 'b.example', 'https://b.example/2026/05/31/cuatro.html',
                'https://b.example/2026/05/31/cuatro.html', '2026-05-31', NULL,
                ?, 100, 'hash-short', 'too_short', 'live')
        """,
        (short_text,),
    )
    conn.execute(
        """
        INSERT INTO pangram_results (article_id, text_hash, response_json, raw_response_json, status)
        VALUES (1, 'hash-live', '{}', '{}', 'ok')
        """
    )
    return conn


def test_validate_rows_metrics_and_warnings() -> None:
    conn = _seed_validation_db()
    rows = database.validation_article_rows(conn, "2026-05-31")
    summary, full, samples = validate_rows(rows, "2026-05-31", sample_size=1)
    media_a = next(row for row in summary if row["media_name"] == "Medio A")
    assert media_a["discovered_urls"] == 3
    assert media_a["extracted_live"] == 1
    assert media_a["recovered_wayback"] == 1
    assert media_a["no_text"] == 1
    assert media_a["sent_to_pangram"] == 1
    assert media_a["avg_wayback_distance_hours"] == 80
    codes = {warning["code"] for warning in full["warnings"]}
    assert "far_wayback_snapshot" in codes
    assert "suspect_urls" in codes
    assert "many_short_texts" in codes
    assert samples[0]["text_preview"] == ""


def test_export_validation_writes_three_files(tmp_path) -> None:
    conn = _seed_validation_db()
    rows = database.validation_article_rows(conn, "2026-05-31")
    _summary, full, samples, summary_path, json_path, sample_path = export_validation(
        rows,
        "2026-05-31",
        sample_size=2,
        output_dir=tmp_path,
    )
    assert summary_path.exists()
    assert json_path.exists()
    assert sample_path.exists()
    assert len(list(csv.DictReader(sample_path.open(encoding="utf-8")))) == len(samples)
    assert full["target_date"] == "2026-05-31"


def test_validate_rows_reports_media_with_no_discovery() -> None:
    rows = [
        {
            "media_name": "Medio C",
            "discovered_id": None,
            "discovered_url": None,
            "article_id": None,
        }
    ]
    summary, full, samples = validate_rows(rows, "2026-05-31", sample_size=10)
    assert summary[0]["media_name"] == "Medio C"
    assert summary[0]["discovered_urls"] == 0
    assert full["warnings"][0]["code"] == "no_discovery"
    assert samples == []
