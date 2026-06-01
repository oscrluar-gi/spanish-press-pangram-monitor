from __future__ import annotations

import csv
import json
import logging
import os
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path

import httpx

from src.discover import (
    RobotsCache,
    entry_matches_target,
    fetch_text,
    load_media_config,
    parse_rss_xml,
    parse_sitemap_xml,
    prioritize_sitemap_index_entries,
    sitemap_candidates_for_media,
)
from src.models import MediaConfig
from src.url_filters import should_include_article_url
from src.utils import DEFAULT_USER_AGENT, parse_target_date

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class SourceAuditRow:
    media_name: str
    domain: str
    source_type: str
    source_url: str
    http_status: int | None
    ok_200: bool
    xml_valid: bool
    is_sitemap_index: bool
    candidate_urls: int
    date_filtered_urls: int
    included_after_url_filters: int
    rss_urls: int
    fallback_urls: int
    error: str | None = None


def audit_sources(config_path: str, target_date: str) -> tuple[list[dict[str, object]], Path, Path]:
    target = parse_target_date(target_date)
    media_items = load_media_config(config_path)
    timeout = float(os.getenv("PRESS_MONITOR_TIMEOUT_SECONDS", "20"))
    user_agent = os.getenv("PRESS_MONITOR_USER_AGENT", DEFAULT_USER_AGENT)
    headers = {"User-Agent": user_agent}
    rows: list[SourceAuditRow] = []

    with httpx.Client(headers=headers, timeout=timeout, follow_redirects=True) as client:
        robots = RobotsCache(client, user_agent)
        for media in media_items:
            LOGGER.info("Auditing sources for %s", media.name)
            for url, _source_name, source_type in sitemap_candidates_for_media(media, target, robots.sitemaps(media.domain)):
                rows.append(_audit_sitemap_source(client, robots, media, target, url, source_type))
            for feed in media.rss_feeds:
                rows.append(_audit_rss_source(client, robots, media, target, feed))

    export_rows = [asdict(row) for row in rows]
    out_dir = Path("exports/audits")
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / f"audit_{target_date}.csv"
    json_path = out_dir / f"audit_{target_date}.json"
    _write_csv(csv_path, export_rows)
    json_path.write_text(json.dumps(export_rows, ensure_ascii=False, indent=2), encoding="utf-8")
    return export_rows, csv_path, json_path


def _audit_sitemap_source(
    client: httpx.Client,
    robots: RobotsCache,
    media: MediaConfig,
    target: date,
    url: str,
    source_type: str,
) -> SourceAuditRow:
    if not robots.can_fetch(media.domain, url):
        return SourceAuditRow(media.name, media.domain, source_type, url, None, False, False, False, 0, 0, 0, 0, int(source_type == "fallback_sitemap"), "robots_disallowed")
    status_code: int | None = None
    try:
        response = client.get(url)
        status_code = response.status_code
        response.raise_for_status()
        content = response.content
        kind, entries = parse_sitemap_xml(content)
        candidates, date_filtered, included = _count_entries(media, entries, target)
        if kind == "sitemapindex":
            nested_candidates = nested_date_filtered = nested_included = 0
            prioritized_entries = prioritize_sitemap_index_entries(entries, target)
            for entry in prioritized_entries[: int(os.getenv("PRESS_MONITOR_MAX_SITEMAPS_PER_DOMAIN", "30"))]:
                try:
                    nested_content = fetch_text(client, entry.loc, retries=0)
                    nested_kind, nested_entries = parse_sitemap_xml(nested_content)
                    if nested_kind == "urlset":
                        c, d, i = _count_entries(media, nested_entries, target)
                        nested_candidates += c
                        nested_date_filtered += d
                        nested_included += i
                except Exception as exc:
                    LOGGER.debug("Nested sitemap audit failed %s: %s", entry.loc, exc)
            candidates = nested_candidates or candidates
            date_filtered = nested_date_filtered
            included = nested_included
        return SourceAuditRow(
            media.name,
            media.domain,
            source_type,
            url,
            status_code,
            status_code == 200,
            True,
            kind == "sitemapindex",
            candidates,
            date_filtered,
            included,
            0,
            included if source_type == "fallback_sitemap" else 0,
        )
    except Exception as exc:
        return SourceAuditRow(media.name, media.domain, source_type, url, status_code, status_code == 200, False, False, 0, 0, 0, 0, 0, str(exc))


def _audit_rss_source(
    client: httpx.Client,
    robots: RobotsCache,
    media: MediaConfig,
    target: date,
    feed: str,
) -> SourceAuditRow:
    url = feed if feed.startswith(("http://", "https://")) else f"https://{media.domain}/{feed.lstrip('/')}"
    if not robots.can_fetch(media.domain, url):
        return SourceAuditRow(media.name, media.domain, "rss", url, None, False, False, False, 0, 0, 0, 0, 0, "robots_disallowed")
    status_code: int | None = None
    try:
        response = client.get(url)
        status_code = response.status_code
        response.raise_for_status()
        entries = parse_rss_xml(response.content)
        candidates, date_filtered, included = _count_entries(media, entries, target)
        return SourceAuditRow(media.name, media.domain, "rss", url, status_code, status_code == 200, True, False, candidates, date_filtered, included, included, 0)
    except Exception as exc:
        return SourceAuditRow(media.name, media.domain, "rss", url, status_code, status_code == 200, False, False, 0, 0, 0, 0, 0, str(exc))


def _count_entries(media: MediaConfig, entries: list[object], target: date) -> tuple[int, int, int]:
    candidates = len(entries)
    date_filtered = 0
    included = 0
    for entry in entries:
        if entry_matches_target(entry, target, allow_month_fallback=media.allow_month_fallback):
            date_filtered += 1
            if should_include_article_url(entry.loc, media).included:
                included += 1
    return candidates, date_filtered, included


def _write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    fieldnames = list(rows[0].keys()) if rows else [field.name for field in SourceAuditRow.__dataclass_fields__.values()]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
