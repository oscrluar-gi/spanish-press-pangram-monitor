from __future__ import annotations

import csv
import json
import logging
import re
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime, time as dt_time, timedelta
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qs, unquote, urlsplit

import httpx

from src.discover import entry_matches_target, load_media_config, parse_rss_xml
from src.models import MediaConfig
from src.url_filters import should_include_article_url
from src.utils import MADRID_TZ, build_request_headers, normalize_article_url, parse_target_date
from src.wayback_client import WAYBACK_CDX_URL, format_wayback_timestamp

LOGGER = logging.getLogger(__name__)
GDELT_DOC_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
DUCKDUCKGO_HTML_URL = "https://duckduckgo.com/html/"
DEFAULT_STRATEGIES = ("wayback_robots", "wayback_rss", "gdelt", "web_search")


@dataclass(frozen=True)
class SourceProbeRow:
    strategy: str
    media_name: str
    domain: str
    source_url: str | None
    candidate_url: str | None
    normalized_url: str | None
    title: str | None
    published_at: str | None
    status: str
    error: str | None = None
    evidence: str | None = None


def probe_sources(
    config_path: str,
    target_date: str,
    *,
    media_filter: str | None = None,
    strategies: Iterable[str] = DEFAULT_STRATEGIES,
    max_results: int = 25,
    timeout_seconds: float = 20.0,
) -> tuple[list[dict[str, Any]], Path, Path]:
    target = parse_target_date(target_date)
    selected_strategies = tuple(_normalize_strategy(value) for value in strategies)
    media_items = _filter_media(load_media_config(config_path), media_filter)
    headers = build_request_headers()
    rows: list[SourceProbeRow] = []
    with httpx.Client(headers=headers, timeout=timeout_seconds, follow_redirects=True) as client:
        for media in media_items:
            LOGGER.info("Probing sources for %s", media.name)
            if "wayback_robots" in selected_strategies:
                rows.extend(probe_wayback_robots(client, media, target, max_results=max_results))
            if "wayback_rss" in selected_strategies:
                rows.extend(probe_wayback_rss(client, media, target, max_results=max_results))
            if "gdelt" in selected_strategies:
                rows.extend(probe_gdelt(client, media, target, max_results=max_results))
            if "web_search" in selected_strategies:
                rows.extend(probe_web_search(client, media, target, max_results=max_results))
            time.sleep(0.5)

    dict_rows = [asdict(row) for row in rows]
    csv_path, json_path = export_probe_rows(dict_rows, target_date, suffix="_".join(selected_strategies))
    return dict_rows, csv_path, json_path


def probe_wayback_robots(
    client: httpx.Client,
    media: MediaConfig,
    target: date,
    *,
    max_results: int,
) -> list[SourceProbeRow]:
    rows: list[SourceProbeRow] = []
    robots_urls = [f"https://www.{media.domain}/robots.txt", f"https://{media.domain}/robots.txt"]
    for robots_url in robots_urls:
        snapshots, error = cdx_snapshots(client, robots_url, target, max_results=3, mimetype_filter=None)
        if error:
            rows.append(_error_row("wayback_robots", media, robots_url, error))
            continue
        for snapshot in snapshots:
            raw_url = wayback_raw_url(snapshot)
            text, fetch_error = fetch_text(client, raw_url)
            if fetch_error:
                rows.append(_error_row("wayback_robots", media, raw_url, fetch_error))
                continue
            sitemaps = parse_robots_sitemaps(text)
            if not sitemaps:
                rows.append(
                    SourceProbeRow(
                        strategy="wayback_robots",
                        media_name=media.name,
                        domain=media.domain,
                        source_url=raw_url,
                        candidate_url=None,
                        normalized_url=None,
                        title=None,
                        published_at=None,
                        status="ok_no_sitemaps",
                        evidence=f"snapshot={snapshot.get('timestamp')}",
                    )
                )
                continue
            for sitemap in sitemaps[:max_results]:
                rows.append(
                    SourceProbeRow(
                        strategy="wayback_robots",
                        media_name=media.name,
                        domain=media.domain,
                        source_url=raw_url,
                        candidate_url=sitemap,
                        normalized_url=normalize_article_url(sitemap),
                        title=None,
                        published_at=None,
                        status="ok",
                        evidence=f"snapshot={snapshot.get('timestamp')}",
                    )
                )
            return rows[:max_results]
    if not rows:
        rows.append(_error_row("wayback_robots", media, None, "no snapshots"))
    return rows[:max_results]


def probe_wayback_rss(
    client: httpx.Client,
    media: MediaConfig,
    target: date,
    *,
    max_results: int,
) -> list[SourceProbeRow]:
    rows: list[SourceProbeRow] = []
    if not media.rss_feeds:
        return [_error_row("wayback_rss", media, None, "no RSS feeds configured")]

    for feed_url in media.rss_feeds:
        snapshots, error = cdx_snapshots(client, feed_url, target, max_results=3, mimetype_filter=None)
        if error:
            rows.append(_error_row("wayback_rss", media, feed_url, error))
            continue
        for snapshot in snapshots:
            raw_url = wayback_raw_url(snapshot)
            text, fetch_error = fetch_text(client, raw_url)
            if fetch_error:
                rows.append(_error_row("wayback_rss", media, raw_url, fetch_error))
                continue
            try:
                entries = parse_rss_xml(text.encode("utf-8", errors="ignore"))
            except Exception as exc:
                rows.append(_error_row("wayback_rss", media, raw_url, f"rss parse error: {exc}"))
                continue
            for entry in entries:
                if not entry_matches_target(entry, target):
                    continue
                filter_result = should_include_article_url(entry.loc, media)
                if not filter_result.included:
                    continue
                rows.append(
                    SourceProbeRow(
                        strategy="wayback_rss",
                        media_name=media.name,
                        domain=media.domain,
                        source_url=raw_url,
                        candidate_url=entry.loc,
                        normalized_url=normalize_article_url(entry.loc),
                        title=None,
                        published_at=entry.publication_date or entry.lastmod,
                        status="ok",
                        evidence=f"snapshot={snapshot.get('timestamp')}",
                    )
                )
                if len(rows) >= max_results:
                    return rows
    if not rows:
        rows.append(_error_row("wayback_rss", media, None, "no matching RSS items"))
    return rows[:max_results]


def probe_gdelt(
    client: httpx.Client,
    media: MediaConfig,
    target: date,
    *,
    max_results: int,
) -> list[SourceProbeRow]:
    rows: list[SourceProbeRow] = []
    for query in (f"domainis:{media.domain}", f"domain:{media.domain}"):
        params = {
            "query": query,
            "mode": "ArtList",
            "format": "json",
            "maxrecords": str(max_results),
            "sort": "DateDesc",
            "startdatetime": f"{target:%Y%m%d}000000",
            "enddatetime": f"{target:%Y%m%d}235959",
        }
        try:
            payload = get_json_with_retry(client, GDELT_DOC_URL, params=params, attempts=2, retry_delay_seconds=8.0)
            articles = parse_gdelt_articles(payload)
        except Exception as exc:
            rows.append(_error_row("gdelt", media, str(httpx.URL(GDELT_DOC_URL, params=params)), str(exc)))
            continue
        for article in articles:
            url = article.get("url")
            if not url or not _url_matches_domain(url, media.domain):
                continue
            filter_result = should_include_article_url(url, media)
            if not filter_result.included:
                continue
            rows.append(
                SourceProbeRow(
                    strategy="gdelt",
                    media_name=media.name,
                    domain=media.domain,
                    source_url=str(httpx.URL(GDELT_DOC_URL, params=params)),
                    candidate_url=url,
                    normalized_url=normalize_article_url(url),
                    title=article.get("title"),
                    published_at=article.get("seendate"),
                    status="ok",
                    evidence=f"query={query}",
                )
            )
            if len(rows) >= max_results:
                return _dedupe_rows(rows)[:max_results]
        if rows:
            return _dedupe_rows(rows)[:max_results]
    if not rows:
        rows.append(_error_row("gdelt", media, None, "no GDELT articles"))
    return rows[:max_results]


def probe_web_search(
    client: httpx.Client,
    media: MediaConfig,
    target: date,
    *,
    max_results: int,
) -> list[SourceProbeRow]:
    query = f"site:{media.domain} {target:%Y-%m-%d}"
    params = {"q": query}
    try:
        response = client.get(DUCKDUCKGO_HTML_URL, params=params)
        response.raise_for_status()
        links = parse_search_result_links(response.text)
    except Exception as exc:
        return [_error_row("web_search", media, str(httpx.URL(DUCKDUCKGO_HTML_URL, params=params)), str(exc))]

    rows: list[SourceProbeRow] = []
    for url in links:
        if not _url_matches_domain(url, media.domain):
            continue
        filter_result = should_include_article_url(url, media)
        if not filter_result.included:
            continue
        rows.append(
            SourceProbeRow(
                strategy="web_search",
                media_name=media.name,
                domain=media.domain,
                source_url=str(httpx.URL(DUCKDUCKGO_HTML_URL, params=params)),
                candidate_url=url,
                normalized_url=normalize_article_url(url),
                title=None,
                published_at=None,
                status="ok_date_unverified",
                evidence=f"query={query}",
            )
        )
        if len(rows) >= max_results:
            break
    if not rows:
        rows.append(_error_row("web_search", media, str(httpx.URL(DUCKDUCKGO_HTML_URL, params=params)), "no search results"))
    return _dedupe_rows(rows)[:max_results]


def cdx_snapshots(
    client: httpx.Client,
    url: str,
    target: date,
    *,
    max_results: int,
    mimetype_filter: str | None,
) -> tuple[list[dict[str, str]], str | None]:
    start = datetime.combine(target, dt_time.min, tzinfo=MADRID_TZ)
    end = start + timedelta(days=1)
    filters = ["statuscode:200"]
    if mimetype_filter:
        filters.append(f"mimetype:{mimetype_filter}")
    params: dict[str, Any] = {
        "url": url,
        "from": format_wayback_timestamp(start),
        "to": format_wayback_timestamp(end),
        "output": "json",
        "filter": filters,
        "collapse": "digest",
        "fl": "timestamp,original,mimetype,statuscode,digest",
        "limit": str(max_results),
    }
    try:
        response = client.get(WAYBACK_CDX_URL, params=params)
        response.raise_for_status()
        return parse_cdx_json(response.json()), None
    except Exception as exc:
        return [], str(exc)


def get_json_with_retry(
    client: httpx.Client,
    url: str,
    *,
    params: dict[str, Any],
    attempts: int,
    retry_delay_seconds: float,
) -> Any:
    last_exc: Exception | None = None
    for attempt in range(attempts):
        try:
            response = client.get(url, params=params)
            if response.status_code == 429 or response.status_code >= 500:
                if attempt < attempts - 1:
                    time.sleep(retry_delay_seconds)
                    continue
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            last_exc = exc
            if attempt < attempts - 1:
                time.sleep(retry_delay_seconds)
                continue
    raise RuntimeError(str(last_exc))


def parse_cdx_json(payload: Any) -> list[dict[str, str]]:
    if not isinstance(payload, list) or not payload:
        return []
    header = payload[0]
    if not isinstance(header, list):
        return []
    rows = []
    for row in payload[1:]:
        if isinstance(row, list):
            rows.append({str(key): str(value) for key, value in zip(header, row)})
    return rows


def parse_robots_sitemaps(text: str) -> list[str]:
    sitemaps: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if line.lower().startswith("sitemap:"):
            value = line.split(":", 1)[1].strip()
            if value:
                sitemaps.append(value)
    return sitemaps


def parse_gdelt_articles(payload: Any) -> list[dict[str, str]]:
    if not isinstance(payload, dict):
        return []
    articles = payload.get("articles")
    if not isinstance(articles, list):
        return []
    result: list[dict[str, str]] = []
    for article in articles:
        if not isinstance(article, dict):
            continue
        result.append({str(key): str(value) for key, value in article.items() if value is not None})
    return result


def parse_search_result_links(html: str) -> list[str]:
    parser = _SearchHTMLParser()
    parser.feed(html)
    result: list[str] = []
    seen: set[str] = set()
    for href in parser.hrefs:
        url = _unwrap_search_url(href)
        if not url or not url.startswith(("http://", "https://")):
            continue
        if url in seen:
            continue
        seen.add(url)
        result.append(url)
    return result


def wayback_raw_url(snapshot: dict[str, str]) -> str:
    return f"https://web.archive.org/web/{snapshot['timestamp']}id_/{snapshot['original']}"


def fetch_text(client: httpx.Client, url: str) -> tuple[str, str | None]:
    try:
        response = client.get(url)
        response.raise_for_status()
        return response.text, None
    except Exception as exc:
        return "", str(exc)


def export_probe_rows(rows: list[dict[str, Any]], target_date: str, suffix: str | None = None) -> tuple[Path, Path]:
    out_dir = Path("exports/source_probes")
    out_dir.mkdir(parents=True, exist_ok=True)
    safe_suffix = f"_{re.sub(r'[^a-zA-Z0-9_]+', '_', suffix)}" if suffix else ""
    csv_path = out_dir / f"source_probe_{target_date}{safe_suffix}.csv"
    json_path = out_dir / f"source_probe_{target_date}{safe_suffix}.json"
    fieldnames = [
        "strategy",
        "media_name",
        "domain",
        "source_url",
        "candidate_url",
        "normalized_url",
        "title",
        "published_at",
        "status",
        "error",
        "evidence",
    ]
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    with json_path.open("w", encoding="utf-8") as handle:
        json.dump(rows, handle, ensure_ascii=False, indent=2)
    return csv_path, json_path


def _filter_media(media_items: list[MediaConfig], media_filter: str | None) -> list[MediaConfig]:
    if not media_filter:
        return media_items
    needles = [part.strip().lower() for part in media_filter.split(",") if part.strip()]
    return [
        media
        for media in media_items
        if any(needle in media.name.lower() or needle in media.domain.lower() for needle in needles)
    ]


def _normalize_strategy(value: str) -> str:
    normalized = value.strip().lower().replace("-", "_")
    if normalized not in DEFAULT_STRATEGIES:
        raise ValueError(f"Unknown probe strategy: {value}")
    return normalized


def _error_row(strategy: str, media: MediaConfig, source_url: str | None, error: str) -> SourceProbeRow:
    return SourceProbeRow(
        strategy=strategy,
        media_name=media.name,
        domain=media.domain,
        source_url=source_url,
        candidate_url=None,
        normalized_url=None,
        title=None,
        published_at=None,
        status="error",
        error=error,
    )


def _dedupe_rows(rows: list[SourceProbeRow]) -> list[SourceProbeRow]:
    result: list[SourceProbeRow] = []
    seen: set[str] = set()
    for row in rows:
        key = row.normalized_url or row.candidate_url or f"{row.strategy}:{row.source_url}:{row.error}"
        if key in seen:
            continue
        seen.add(key)
        result.append(row)
    return result


def _url_matches_domain(url: str, domain: str) -> bool:
    host = urlsplit(url).netloc.lower()
    domain = domain.lower()
    return host == domain or host.endswith(f".{domain}")


def _unwrap_search_url(href: str) -> str | None:
    if href.startswith("//"):
        href = f"https:{href}"
    parsed = urlsplit(href)
    query = parse_qs(parsed.query)
    if "uddg" in query:
        return unquote(query["uddg"][0])
    if parsed.netloc and not re.search(r"(^|\.)duckduckgo\.com$", parsed.netloc.lower()):
        return href
    return None


class _SearchHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        data = {key.lower(): value or "" for key, value in attrs}
        href = data.get("href")
        if href:
            self.hrefs.append(href)
