from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from src.models import DiscoveredURL, ExtractedArticle, MediaConfig
from src.wayback_client import WaybackSnapshot, build_wayback_raw_url
from src.utils import ensure_parent_dir, json_dumps, normalize_article_url, response_model_version, response_summary

DEFAULT_DB_PATH = "data/press_monitor.sqlite"


def get_db_path(path: str | None = None) -> str:
    return path or os.getenv("PRESS_MONITOR_DB", DEFAULT_DB_PATH)


def connect(path: str | None = None) -> sqlite3.Connection:
    db_path = get_db_path(path)
    ensure_parent_dir(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def initialize_database(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS media (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            domain TEXT NOT NULL UNIQUE,
            sitemap_urls_json TEXT NOT NULL DEFAULT '[]',
            rss_feeds_json TEXT NOT NULL DEFAULT '[]',
            filters_json TEXT NOT NULL DEFAULT '{}',
            request_delay_seconds REAL,
            max_concurrency_per_domain INTEGER,
            max_retries INTEGER,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS discovered_urls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            media_id INTEGER NOT NULL REFERENCES media(id),
            media_name TEXT NOT NULL,
            domain TEXT NOT NULL,
            url TEXT NOT NULL,
            normalized_url TEXT NOT NULL,
            discovered_from TEXT NOT NULL,
            lastmod TEXT,
            discovered_lastmod TEXT,
            rss_published_at TEXT,
            source_type TEXT NOT NULL DEFAULT 'sitemap',
            filter_status TEXT NOT NULL DEFAULT 'included',
            filter_reason TEXT,
            target_date TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(normalized_url),
            UNIQUE(target_date, media_id, normalized_url)
        );

        CREATE TABLE IF NOT EXISTS articles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            discovered_url_id INTEGER NOT NULL UNIQUE REFERENCES discovered_urls(id),
            media_id INTEGER NOT NULL REFERENCES media(id),
            media_name TEXT NOT NULL,
            domain TEXT NOT NULL,
            url TEXT NOT NULL,
            normalized_url TEXT NOT NULL,
            target_date TEXT NOT NULL,
            published_at TEXT,
            article_published_at TEXT,
            article_modified_at TEXT,
            title TEXT,
            author TEXT,
            section TEXT,
            tags_json TEXT NOT NULL DEFAULT '[]',
            canonical_url TEXT,
            language TEXT,
            is_paywalled INTEGER NOT NULL DEFAULT 0,
            text_clean TEXT,
            word_count INTEGER NOT NULL DEFAULT 0,
            text_hash TEXT,
            extraction_status TEXT NOT NULL,
            skip_reason TEXT,
            error TEXT,
            content_source TEXT NOT NULL DEFAULT 'live',
            source_url TEXT,
            original_url TEXT,
            wayback_timestamp TEXT,
            wayback_distance_seconds INTEGER,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(normalized_url),
            UNIQUE(target_date, media_id, normalized_url)
        );

        CREATE INDEX IF NOT EXISTS idx_articles_text_hash ON articles(text_hash);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_articles_text_hash_unique
            ON articles(text_hash) WHERE text_hash IS NOT NULL;
        CREATE INDEX IF NOT EXISTS idx_articles_target_date ON articles(target_date);

        CREATE TABLE IF NOT EXISTS pangram_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            article_id INTEGER NOT NULL UNIQUE REFERENCES articles(id),
            text_hash TEXT,
            response_json TEXT,
            raw_response_json TEXT,
            prediction TEXT,
            score TEXT,
            pangram_model_version TEXT,
            status TEXT NOT NULL,
            error TEXT,
            error_message TEXT,
            analyzed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_pangram_text_hash ON pangram_results(text_hash);

        CREATE TABLE IF NOT EXISTS wayback_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            normalized_url TEXT NOT NULL,
            original_url TEXT NOT NULL,
            snapshot_url TEXT,
            raw_snapshot_url TEXT,
            timestamp TEXT NOT NULL,
            statuscode TEXT,
            mimetype TEXT,
            digest TEXT,
            source_api TEXT NOT NULL,
            distance_seconds INTEGER,
            selected INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            error_message TEXT,
            UNIQUE(normalized_url, timestamp, digest)
        );

        CREATE TABLE IF NOT EXISTS run_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_type TEXT NOT NULL,
            target_date TEXT,
            status TEXT NOT NULL,
            message TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        """
    )
    _run_lightweight_migrations(conn)
    conn.commit()


def _run_lightweight_migrations(conn: sqlite3.Connection) -> None:
    expected = {
        "media": {
            "sitemap_urls_json": "TEXT NOT NULL DEFAULT '[]'",
            "filters_json": "TEXT NOT NULL DEFAULT '{}'",
            "request_delay_seconds": "REAL",
            "max_concurrency_per_domain": "INTEGER",
            "max_retries": "INTEGER",
        },
        "discovered_urls": {
            "discovered_lastmod": "TEXT",
            "rss_published_at": "TEXT",
            "source_type": "TEXT NOT NULL DEFAULT 'sitemap'",
            "filter_status": "TEXT NOT NULL DEFAULT 'included'",
            "filter_reason": "TEXT",
        },
        "articles": {
            "article_published_at": "TEXT",
            "article_modified_at": "TEXT",
            "section": "TEXT",
            "tags_json": "TEXT NOT NULL DEFAULT '[]'",
            "canonical_url": "TEXT",
            "language": "TEXT",
            "is_paywalled": "INTEGER NOT NULL DEFAULT 0",
            "skip_reason": "TEXT",
            "content_source": "TEXT NOT NULL DEFAULT 'live'",
            "source_url": "TEXT",
            "original_url": "TEXT",
            "wayback_timestamp": "TEXT",
            "wayback_distance_seconds": "INTEGER",
        },
        "pangram_results": {
            "raw_response_json": "TEXT",
            "pangram_model_version": "TEXT",
            "error_message": "TEXT",
        },
    }
    for table, columns in expected.items():
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, definition in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_articles_text_hash_unique "
        "ON articles(text_hash) WHERE text_hash IS NOT NULL"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_discovered_normalized_url_unique "
        "ON discovered_urls(normalized_url)"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_articles_normalized_url_unique "
        "ON articles(normalized_url)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS wayback_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            normalized_url TEXT NOT NULL,
            original_url TEXT NOT NULL,
            snapshot_url TEXT,
            raw_snapshot_url TEXT,
            timestamp TEXT NOT NULL,
            statuscode TEXT,
            mimetype TEXT,
            digest TEXT,
            source_api TEXT NOT NULL,
            distance_seconds INTEGER,
            selected INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            error_message TEXT,
            UNIQUE(normalized_url, timestamp, digest)
        )
        """
    )


def upsert_media(conn: sqlite3.Connection, media: MediaConfig) -> int:
    conn.execute(
        """
        INSERT INTO media
            (name, domain, sitemap_urls_json, rss_feeds_json, filters_json,
             request_delay_seconds, max_concurrency_per_domain, max_retries)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(domain) DO UPDATE SET
            name = excluded.name,
            sitemap_urls_json = excluded.sitemap_urls_json,
            rss_feeds_json = excluded.rss_feeds_json,
            filters_json = excluded.filters_json,
            request_delay_seconds = excluded.request_delay_seconds,
            max_concurrency_per_domain = excluded.max_concurrency_per_domain,
            max_retries = excluded.max_retries
        """,
        (
            media.name,
            media.domain,
            json_dumps(media.sitemap_urls),
            json_dumps(media.rss_feeds),
            json_dumps(
                {
                    "include_url_patterns": media.include_url_patterns,
                    "exclude_url_patterns": media.exclude_url_patterns,
                    "include_liveblogs": media.include_liveblogs,
                    "include_opinion": media.include_opinion,
                    "include_sports": media.include_sports,
                    "allow_month_fallback": media.allow_month_fallback,
                    "wayback_discovery_min_candidates": media.wayback_discovery_min_candidates,
                    "wayback_discovery": media.wayback_discovery,
                    "wayback_discovery_limit": media.wayback_discovery_limit,
                    "wayback_discovery_patterns": media.wayback_discovery_patterns,
                    "wayback_discovery_broad": media.wayback_discovery_broad,
                    "gdelt_discovery": media.gdelt_discovery,
                    "gdelt_discovery_limit": media.gdelt_discovery_limit,
                }
            ),
            media.request_delay_seconds,
            media.max_concurrency_per_domain,
            media.max_retries,
        ),
    )
    row = conn.execute("SELECT id FROM media WHERE domain = ?", (media.domain,)).fetchone()
    conn.commit()
    return int(row["id"])


def upsert_media_many(conn: sqlite3.Connection, media_items: Iterable[MediaConfig]) -> None:
    for media in media_items:
        upsert_media(conn, media)


def save_discovered_url(conn: sqlite3.Connection, item: DiscoveredURL) -> bool:
    media_id = get_or_create_media(conn, item.media_name, item.domain)
    normalized = normalize_article_url(item.url)
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO discovered_urls
            (media_id, media_name, domain, url, normalized_url, discovered_from, lastmod,
             discovered_lastmod, rss_published_at, source_type, filter_status, filter_reason, target_date)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            media_id,
            item.media_name,
            item.domain,
            item.url,
            normalized,
            item.discovered_from,
            item.discovered_lastmod,
            item.discovered_lastmod,
            item.rss_published_at,
            item.source_type,
            item.filter_status,
            item.filter_reason,
            item.target_date,
        ),
    )
    conn.commit()
    return cur.rowcount > 0


def get_or_create_media(conn: sqlite3.Connection, name: str, domain: str) -> int:
    conn.execute(
        """
        INSERT INTO media (name, domain, rss_feeds_json)
        VALUES (?, ?, '[]')
        ON CONFLICT(domain) DO UPDATE SET name = excluded.name
        """,
        (name, domain),
    )
    row = conn.execute("SELECT id FROM media WHERE domain = ?", (domain,)).fetchone()
    conn.commit()
    return int(row["id"])


def discovered_for_date(conn: sqlite3.Connection, target_date: str) -> list[sqlite3.Row]:
    return list(
        conn.execute(
        """
            SELECT d.*, m.id AS media_id, a.extraction_status AS current_extraction_status
            FROM discovered_urls d
            JOIN media m ON m.id = d.media_id
            LEFT JOIN articles a ON a.discovered_url_id = d.id
            WHERE d.target_date = ?
            ORDER BY d.media_name, d.url
            """,
            (target_date,),
        )
    )


def save_article(conn: sqlite3.Connection, discovered_url_id: int, media_id: int, media_name: str, domain: str, article: ExtractedArticle) -> None:
    try:
        _save_article_row(conn, discovered_url_id, media_id, media_name, domain, article)
    except sqlite3.IntegrityError as exc:
        if article.text_hash and "text_hash" in str(exc).lower():
            duplicate_article = ExtractedArticle(
                url=article.url,
                normalized_url=article.normalized_url,
                target_date=article.target_date,
                title=article.title,
                author=article.author,
                article_published_at=article.article_published_at,
                article_modified_at=article.article_modified_at,
                section=article.section,
                tags=article.tags,
                canonical_url=article.canonical_url,
                language=article.language,
                is_paywalled=article.is_paywalled,
                text_clean=None,
                word_count=article.word_count,
                text_hash=None,
                extraction_status="paywall_or_incomplete",
                error=f"Duplicate text hash already stored: {article.text_hash}",
            )
            _save_article_row(conn, discovered_url_id, media_id, media_name, domain, duplicate_article)
        else:
            raise
    conn.commit()


def _save_article_row(
    conn: sqlite3.Connection,
    discovered_url_id: int,
    media_id: int,
    media_name: str,
    domain: str,
    article: ExtractedArticle,
) -> None:
    conn.execute(
        """
        INSERT INTO articles
            (discovered_url_id, media_id, media_name, domain, url, normalized_url, target_date,
             published_at, article_published_at, article_modified_at, title, author, section, tags_json,
             canonical_url, language, is_paywalled, text_clean, word_count, text_hash, extraction_status,
             skip_reason, error, content_source, source_url, original_url, wayback_timestamp, wayback_distance_seconds)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(discovered_url_id) DO UPDATE SET
            published_at = excluded.published_at,
            article_published_at = excluded.article_published_at,
            article_modified_at = excluded.article_modified_at,
            title = excluded.title,
            author = excluded.author,
            section = excluded.section,
            tags_json = excluded.tags_json,
            canonical_url = excluded.canonical_url,
            language = excluded.language,
            is_paywalled = excluded.is_paywalled,
            text_clean = excluded.text_clean,
            word_count = excluded.word_count,
            text_hash = excluded.text_hash,
            extraction_status = excluded.extraction_status,
            skip_reason = excluded.skip_reason,
            error = excluded.error,
            content_source = excluded.content_source,
            source_url = excluded.source_url,
            original_url = excluded.original_url,
            wayback_timestamp = excluded.wayback_timestamp,
            wayback_distance_seconds = excluded.wayback_distance_seconds,
            updated_at = CURRENT_TIMESTAMP
        """,
        (
            discovered_url_id,
            media_id,
            media_name,
            domain,
            article.url,
            article.normalized_url,
            article.target_date,
            _scalar_or_json(article.published_at),
            _scalar_or_json(article.article_published_at),
            _scalar_or_json(article.article_modified_at),
            _scalar_or_json(article.title),
            _scalar_or_json(article.author),
            _scalar_or_json(article.section),
            json_dumps(article.tags),
            _scalar_or_json(article.canonical_url),
            _scalar_or_json(article.language),
            1 if article.is_paywalled else 0,
            article.text_clean,
            article.word_count,
            article.text_hash,
            article.extraction_status,
            article.extraction_status if article.extraction_status not in {"ok", "ok_live", "ok_wayback"} else None,
            article.error,
            article.content_source,
            article.source_url,
            article.original_url,
            article.wayback_timestamp,
            article.wayback_distance_seconds,
        ),
    )


def _scalar_or_json(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        return str(value)
    return json_dumps(value)


def articles_ready_for_analysis(
    conn: sqlite3.Connection,
    target_date: str,
    *,
    include_incomplete: bool = False,
    force: bool = False,
    limit: int | None = None,
) -> list[sqlite3.Row]:
    statuses = ["ok", "ok_live", "ok_wayback"]
    if include_incomplete:
        statuses.extend(["too_short", "paywall_or_incomplete", "wayback_too_short"])
    status_placeholders = ",".join("?" for _ in statuses)
    result_filter = "" if force else "AND p.id IS NULL"
    limit_sql = f"LIMIT {int(limit)}" if limit else ""
    return list(
        conn.execute(
            f"""
            SELECT a.*
            FROM articles a
            LEFT JOIN pangram_results p ON p.article_id = a.id
            WHERE a.target_date = ?
              AND a.extraction_status IN ({status_placeholders})
              AND a.text_clean IS NOT NULL
              AND a.text_hash IS NOT NULL
              {result_filter}
            ORDER BY a.media_name, a.url
            {limit_sql}
            """,
            (target_date, *statuses),
        )
    )


def pangram_result_for_hash(conn: sqlite3.Connection, text_hash: str) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT * FROM pangram_results
        WHERE text_hash = ? AND raw_response_json IS NOT NULL AND status IN ('ok', 'reused')
        ORDER BY analyzed_at DESC
        LIMIT 1
        """,
        (text_hash,),
    ).fetchone()


def pangram_status_counts_for_date(conn: sqlite3.Connection, target_date: str) -> dict[str, int]:
    rows = conn.execute(
        """
        SELECT p.status, COUNT(*) AS count
        FROM pangram_results p
        JOIN articles a ON a.id = p.article_id
        WHERE a.target_date = ?
        GROUP BY p.status
        """,
        (target_date,),
    ).fetchall()
    return {str(row["status"]): int(row["count"]) for row in rows}


def save_pangram_result(
    conn: sqlite3.Connection,
    article_id: int,
    text_hash: str | None,
    response: dict[str, Any] | None,
    status: str,
    error: str | None = None,
) -> None:
    prediction, score = response_summary(response)
    model_version = response_model_version(response)
    response_json = json_dumps(response) if response is not None else None
    conn.execute(
        """
        INSERT INTO pangram_results
            (article_id, text_hash, response_json, raw_response_json, prediction, score,
             pangram_model_version, status, error, error_message)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(article_id) DO UPDATE SET
            text_hash = excluded.text_hash,
            response_json = excluded.response_json,
            raw_response_json = excluded.raw_response_json,
            prediction = excluded.prediction,
            score = excluded.score,
            pangram_model_version = excluded.pangram_model_version,
            status = excluded.status,
            error = excluded.error,
            error_message = excluded.error_message,
            analyzed_at = CURRENT_TIMESTAMP
        """,
        (
            article_id,
            text_hash,
            response_json,
            response_json,
            str(prediction) if prediction is not None else None,
            str(score) if score is not None else None,
            str(model_version) if model_version is not None else None,
            status,
            error,
            error,
        ),
    )
    conn.commit()


def purge_article_text(conn: sqlite3.Connection, article_id: int) -> None:
    conn.execute(
        """
        UPDATE articles
        SET text_clean = NULL,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (article_id,),
    )
    conn.commit()


def log_run(conn: sqlite3.Connection, run_type: str, target_date: str | None, status: str, message: str | None = None) -> None:
    conn.execute(
        "INSERT INTO run_log (run_type, target_date, status, message) VALUES (?, ?, ?, ?)",
        (run_type, target_date, status, message),
    )
    conn.commit()


def export_rows(conn: sqlite3.Connection, target_date: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            a.media_name,
            a.url,
            a.title,
            COALESCE(a.article_published_at, a.published_at) AS published_at,
            a.word_count,
            a.extraction_status,
            p.prediction AS pangram_prediction,
            p.score AS pangram_score,
            COALESCE(p.raw_response_json, p.response_json) AS pangram_response_json
        FROM articles a
        LEFT JOIN pangram_results p ON p.article_id = a.id
        WHERE a.target_date = ?
        ORDER BY a.media_name, a.published_at, a.url
        """,
        (target_date,),
    ).fetchall()
    return [dict(row) for row in rows]


def report_rows(conn: sqlite3.Connection, target_date: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            d.media_name,
            COUNT(DISTINCT d.id) AS discovered,
            COUNT(DISTINCT CASE WHEN a.extraction_status IN ('ok', 'ok_live', 'ok_wayback') THEN a.id END) AS extracted_ok,
            COUNT(DISTINCT CASE WHEN a.content_source = 'live' AND a.extraction_status IN ('ok', 'ok_live') THEN a.id END) AS live_ok,
            COUNT(DISTINCT CASE WHEN a.content_source = 'wayback' AND a.extraction_status = 'ok_wayback' THEN a.id END) AS wayback_ok,
            COUNT(DISTINCT CASE WHEN ws.id IS NOT NULL THEN ws.id END) AS wayback_hits,
            COUNT(DISTINCT CASE WHEN a.extraction_status IN ('wayback_not_found', 'wayback_fetch_error', 'wayback_parse_error', 'wayback_too_short') THEN a.id END) AS wayback_misses,
            AVG(CASE WHEN a.content_source = 'wayback' THEN a.wayback_distance_seconds END) AS avg_wayback_distance_seconds,
            COUNT(DISTINCT CASE WHEN p.status IN ('ok', 'reused') THEN p.id END) AS pangram_sent,
            COUNT(DISTINCT CASE WHEN a.extraction_status IS NOT NULL AND a.extraction_status NOT IN ('ok', 'ok_live', 'ok_wayback') THEN a.id END) AS extraction_failures
        FROM discovered_urls d
        LEFT JOIN articles a ON a.discovered_url_id = d.id
        LEFT JOIN pangram_results p ON p.article_id = a.id
        LEFT JOIN wayback_snapshots ws ON ws.normalized_url = d.normalized_url AND ws.selected = 1
        WHERE d.target_date = ?
        GROUP BY d.media_name
        ORDER BY discovered DESC
        """,
        (target_date,),
    ).fetchall()
    return [dict(row) for row in rows]


def error_rows(conn: sqlite3.Connection, target_date: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            a.media_name,
            a.url,
            a.extraction_status,
            CASE WHEN p.status = 'error' THEN 'pangram_error' ELSE a.extraction_status END AS error_type,
            COALESCE(a.skip_reason, a.error, p.error_message) AS reason
        FROM articles a
        LEFT JOIN pangram_results p ON p.article_id = a.id
        WHERE a.target_date = ?
          AND (a.extraction_status NOT IN ('ok', 'ok_live', 'ok_wayback') OR p.status = 'error')
        ORDER BY a.media_name, a.extraction_status, a.url
        """,
        (target_date,),
    ).fetchall()
    return [dict(row) for row in rows]


def validation_article_rows(conn: sqlite3.Connection, target_date: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            m.name AS media_name,
            d.id AS discovered_id,
            d.url AS discovered_url,
            d.normalized_url AS discovered_normalized_url,
            d.filter_status,
            d.filter_reason,
            a.id AS article_id,
            a.url,
            a.title,
            COALESCE(a.article_published_at, a.published_at) AS published_at,
            a.word_count,
            a.extraction_status,
            a.content_source,
            a.wayback_timestamp,
            a.wayback_distance_seconds,
            a.text_clean,
            a.skip_reason,
            a.error,
            a.text_hash,
            p.id AS pangram_result_id,
            p.status AS pangram_status
        FROM media m
        LEFT JOIN discovered_urls d ON d.media_id = m.id AND d.target_date = ?
        LEFT JOIN articles a ON a.discovered_url_id = d.id
        LEFT JOIN pangram_results p ON p.article_id = a.id
        ORDER BY m.name, d.url
        """,
        (target_date,),
    ).fetchall()
    return [dict(row) for row in rows]


def table_counts(conn: sqlite3.Connection, target_date: str | None = None) -> dict[str, int]:
    if target_date is None:
        return {
            "media": conn.execute("SELECT COUNT(*) FROM media").fetchone()[0],
            "discovered_urls": conn.execute("SELECT COUNT(*) FROM discovered_urls").fetchone()[0],
            "articles": conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0],
            "pangram_results": conn.execute("SELECT COUNT(*) FROM pangram_results").fetchone()[0],
            "wayback_snapshots": conn.execute("SELECT COUNT(*) FROM wayback_snapshots").fetchone()[0],
        }
    return {
        "media": conn.execute("SELECT COUNT(*) FROM media").fetchone()[0],
        "discovered_urls": conn.execute("SELECT COUNT(*) FROM discovered_urls WHERE target_date = ?", (target_date,)).fetchone()[0],
        "articles": conn.execute("SELECT COUNT(*) FROM articles WHERE target_date = ?", (target_date,)).fetchone()[0],
        "pangram_results": conn.execute(
            """
            SELECT COUNT(*)
            FROM pangram_results p
            JOIN articles a ON a.id = p.article_id
            WHERE a.target_date = ?
            """,
            (target_date,),
        ).fetchone()[0],
        "wayback_snapshots": conn.execute(
            """
            SELECT COUNT(*)
            FROM wayback_snapshots ws
            JOIN discovered_urls d ON d.normalized_url = ws.normalized_url
            WHERE d.target_date = ?
            """,
            (target_date,),
        ).fetchone()[0],
    }


def has_no_ingested_data(conn: sqlite3.Connection, target_date: str) -> bool:
    counts = table_counts(conn, target_date)
    return counts["discovered_urls"] == 0 and counts["articles"] == 0 and counts["pangram_results"] == 0


def latest_run_log(conn: sqlite3.Connection, limit: int = 10) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT run_type, target_date, status, message, created_at
        FROM run_log
        ORDER BY id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(row) for row in rows]


def save_wayback_snapshot(
    conn: sqlite3.Connection,
    normalized_url: str,
    snapshot: WaybackSnapshot,
    *,
    selected: bool,
    error_message: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO wayback_snapshots
            (normalized_url, original_url, snapshot_url, raw_snapshot_url, timestamp, statuscode,
             mimetype, digest, source_api, distance_seconds, selected, error_message)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(normalized_url, timestamp, digest) DO UPDATE SET
            snapshot_url = excluded.snapshot_url,
            raw_snapshot_url = excluded.raw_snapshot_url,
            statuscode = excluded.statuscode,
            mimetype = excluded.mimetype,
            source_api = excluded.source_api,
            distance_seconds = excluded.distance_seconds,
            selected = MAX(wayback_snapshots.selected, excluded.selected),
            error_message = excluded.error_message
        """,
        (
            normalized_url,
            snapshot.original_url,
            snapshot.snapshot_url,
            build_wayback_raw_url(snapshot),
            snapshot.timestamp,
            str(snapshot.statuscode) if snapshot.statuscode is not None else None,
            snapshot.mimetype,
            snapshot.digest or "",
            snapshot.source_api,
            snapshot.distance_seconds,
            1 if selected else 0,
            error_message or snapshot.error_message,
        ),
    )
    conn.commit()


def wayback_snapshot_exists(conn: sqlite3.Connection, normalized_url: str, timestamp: str, digest: str | None) -> bool:
    row = conn.execute(
        "SELECT id FROM wayback_snapshots WHERE normalized_url = ? AND timestamp = ? AND digest = ?",
        (normalized_url, timestamp, digest or ""),
    ).fetchone()
    return row is not None
