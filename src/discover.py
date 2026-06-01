from __future__ import annotations

import gzip
import logging
import os
import random
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Iterable
from urllib.parse import urljoin
from urllib.robotparser import RobotFileParser

import httpx
import yaml

from src.gdelt_client import GdeltArticle, GdeltClient, load_gdelt_config
from src.models import DiscoveredURL, MediaConfig, WaybackConfig
from src.url_filters import should_include_article_url
from src.utils import (
    DEFAULT_USER_AGENT,
    MADRID_TZ,
    build_request_headers,
    datetime_matches_target,
    datetime_in_window,
    normalize_article_url,
    parse_datetime,
    parse_target_date,
    start_end_for_date,
    url_date_hint,
    url_month_hint,
)
from src.wayback_client import WaybackClient, WaybackSnapshot, load_wayback_config, timestamp_to_datetime

LOGGER = logging.getLogger(__name__)
SITEMAP_CANDIDATES = ("sitemap.xml", "sitemap_index.xml", "news-sitemap.xml")


@dataclass(frozen=True)
class SitemapEntry:
    loc: str
    lastmod: str | None = None
    publication_date: str | None = None


class RobotsCache:
    def __init__(self, client: httpx.Client, user_agent: str) -> None:
        self.client = client
        self.user_agent = user_agent
        self._cache: dict[str, RobotFileParser] = {}
        self._sitemaps: dict[str, list[str]] = {}

    def can_fetch(self, domain: str, url: str) -> bool:
        if os.getenv("PRESS_MONITOR_RESPECT_ROBOTS", "true").lower() not in {"1", "true", "yes"}:
            return True
        parser = self._cache.get(domain)
        if parser is None:
            parser = RobotFileParser()
            parser.set_url(f"https://{domain}/robots.txt")
            try:
                response = self.client.get(f"https://{domain}/robots.txt")
                if response.status_code < 400:
                    parser.parse(response.text.splitlines())
                    self._sitemaps[domain] = _robots_sitemaps(response.text)
                else:
                    parser.parse([])
                    self._sitemaps[domain] = []
            except httpx.HTTPError:
                parser.parse([])
                self._sitemaps[domain] = []
            self._cache[domain] = parser
        return parser.can_fetch(self.user_agent, url)

    def sitemaps(self, domain: str) -> list[str]:
        self.can_fetch(domain, f"https://{domain}/")
        return self._sitemaps.get(domain, [])


def load_media_config(path: str = "config/media.yaml") -> list[MediaConfig]:
    with open(path, "r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    items = raw.get("media", [])
    return [
        MediaConfig(
            name=item["name"],
            domain=item["domain"].replace("https://", "").replace("http://", "").strip("/"),
            sitemap_urls=list(item.get("sitemap_urls") or []),
            sitemap_page_range=_load_page_range(item.get("sitemap_page_range")),
            rss_feeds=list(item.get("rss_feeds") or []),
            include_url_patterns=list(item.get("include_url_patterns") or []),
            exclude_url_patterns=list(item.get("exclude_url_patterns") or []),
            include_liveblogs=bool(item.get("include_liveblogs", False)),
            include_opinion=bool(item.get("include_opinion", True)),
            include_sports=bool(item.get("include_sports", True)),
            allow_month_fallback=bool(item.get("allow_month_fallback", False)),
            wayback_discovery=bool(item.get("wayback_discovery", False)),
            wayback_discovery_min_candidates=_optional_int(item.get("wayback_discovery_min_candidates")),
            wayback_discovery_limit=_optional_int(item.get("wayback_discovery_limit")),
            wayback_discovery_patterns=list(item.get("wayback_discovery_patterns") or []),
            wayback_discovery_broad=bool(item.get("wayback_discovery_broad", False)),
            gdelt_discovery=bool(item.get("gdelt_discovery", True)),
            gdelt_discovery_limit=_optional_int(item.get("gdelt_discovery_limit")),
            request_delay_seconds=_optional_float(item.get("request_delay_seconds")),
            max_concurrency_per_domain=_optional_int(item.get("max_concurrency_per_domain")),
            max_retries=_optional_int(item.get("max_retries")),
        )
        for item in items
    ]


def fetch_text(client: httpx.Client, url: str, retries: int = 2) -> bytes:
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            response = client.get(url)
            response.raise_for_status()
            content = response.content
            if content.startswith(b"\x1f\x8b"):
                return gzip.decompress(content)
            return content
        except (httpx.HTTPError, OSError) as exc:
            last_exc = exc
            if attempt < retries and _is_retryable_fetch_error(exc):
                delay = backoff_delay(attempt)
                LOGGER.info("Retrying %s in %.2fs after %s", url, delay, exc)
                time.sleep(delay)
                continue
            break
    assert last_exc is not None
    raise last_exc


def parse_sitemap_xml(xml_bytes: bytes) -> tuple[str, list[SitemapEntry]]:
    root = ET.fromstring(xml_bytes)
    tag = _local_name(root.tag)
    if tag not in {"urlset", "sitemapindex"}:
        raise ValueError(f"Unsupported sitemap root: {tag}")

    entries: list[SitemapEntry] = []
    child_name = "url" if tag == "urlset" else "sitemap"
    for node in root:
        if _local_name(node.tag) != child_name:
            continue
        loc = _child_text(node, "loc")
        if not loc:
            continue
        entries.append(
            SitemapEntry(
                loc=loc.strip(),
                lastmod=_child_text(node, "lastmod"),
                publication_date=_descendant_text(node, "publication_date"),
            )
        )
    return tag, entries


def parse_rss_xml(xml_bytes: bytes) -> list[SitemapEntry]:
    root = ET.fromstring(xml_bytes)
    entries: list[SitemapEntry] = []
    for node in root.iter():
        if _local_name(node.tag) not in {"item", "entry"}:
            continue
        loc = _child_text(node, "link")
        if not loc:
            link_node = next((child for child in node if _local_name(child.tag) == "link"), None)
            loc = link_node.attrib.get("href") if link_node is not None else None
        if not loc:
            continue
        published = _child_text(node, "pubDate") or _child_text(node, "published") or _child_text(node, "updated")
        entries.append(SitemapEntry(loc=loc.strip(), lastmod=published, publication_date=published))
    return entries


def discover_media(
    media: MediaConfig,
    target: date,
    client: httpx.Client,
    robots: RobotsCache,
    time_window: tuple[datetime, datetime] | None = None,
) -> list[DiscoveredURL]:
    found: dict[str, DiscoveredURL] = {}
    max_sitemaps = int(os.getenv("PRESS_MONITOR_MAX_SITEMAPS_PER_DOMAIN", "30"))
    max_urls = int(os.getenv("PRESS_MONITOR_MAX_URLS_PER_DOMAIN", "20000"))
    max_retries = media.max_retries if media.max_retries is not None else int(os.getenv("PRESS_MONITOR_MAX_RETRIES", "2"))

    sitemap_candidates = sitemap_candidates_for_media(media, target, robots.sitemaps(media.domain))

    for sitemap_url, source_name, source_type in sitemap_candidates:
        if not robots.can_fetch(media.domain, sitemap_url):
            LOGGER.info("robots.txt disallows %s", sitemap_url)
            continue
        try:
            _discover_sitemap_url(
                media,
                sitemap_url,
                source_name,
                target,
                client,
                robots,
                found,
                time_window=time_window,
                max_sitemaps=max_sitemaps,
                max_urls=max_urls,
                source_type=source_type,
                max_retries=max_retries,
            )
        except Exception as exc:
            LOGGER.debug("Sitemap candidate failed %s: %s", sitemap_url, exc)

    for feed in media.rss_feeds:
        feed_url = feed if feed.startswith(("http://", "https://")) else urljoin(f"https://{media.domain}/", feed)
        if not robots.can_fetch(media.domain, feed_url):
            LOGGER.info("robots.txt disallows %s", feed_url)
            continue
        try:
            content = fetch_text(client, feed_url, retries=max_retries)
            for entry in parse_rss_xml(content):
                if entry_matches_target(entry, target, time_window=time_window, allow_month_fallback=media.allow_month_fallback):
                    _add_discovered_entry(found, media, entry, feed_url, target, "rss")
        except Exception as exc:
            LOGGER.debug("RSS feed failed %s: %s", feed_url, exc)

    return list(found.values())


def sitemap_candidates_for_media(media: MediaConfig, target: date, robots_sitemaps: list[str] | None = None) -> list[tuple[str, str, str]]:
    configured = [(url, source, "configured_sitemap") for url, source in expand_sitemap_urls(media, target)]
    robots = [(url, "robots.txt", "robots_sitemap") for url in robots_sitemaps or []]
    fallback = [(f"https://{media.domain}/{candidate}", candidate, "fallback_sitemap") for candidate in SITEMAP_CANDIDATES]
    return _dedupe_sitemap_candidates(configured + robots + fallback)


def expand_sitemap_urls(media: MediaConfig, target: date) -> list[tuple[str, str]]:
    expanded: list[tuple[str, str]] = []
    replacements = {
        "year": f"{target.year:04d}",
        "month": f"{target.month:02d}",
        "day": f"{target.day:02d}",
    }
    for template in media.sitemap_urls:
        absolute_template = template if template.startswith(("http://", "https://")) else urljoin(f"https://{media.domain}/", template)
        if "{page}" in absolute_template:
            start, end = media.sitemap_page_range or (0, 0)
            for page in range(start, end + 1):
                values = {**replacements, "page": str(page)}
                url = absolute_template.format(**values)
                expanded.append((url, template))
            continue
        expanded.append((absolute_template.format(**replacements), template))
    return expanded


def _dedupe_sitemap_candidates(candidates: list[tuple[str, str, str]]) -> list[tuple[str, str, str]]:
    seen: set[str] = set()
    result: list[tuple[str, str, str]] = []
    for url, source, source_type in candidates:
        if url in seen:
            continue
        seen.add(url)
        result.append((url, source, source_type))
    return result


def discover_all(
    config_path: str,
    target_date: str,
    time_window: tuple[datetime, datetime] | None = None,
    media_filter: str | None = None,
) -> list[DiscoveredURL]:
    target = parse_target_date(target_date)
    media_items = load_media_config(config_path)
    if media_filter:
        needle = media_filter.lower()
        media_items = [media for media in media_items if needle in media.name.lower() or needle in media.domain.lower()]
    wayback_config = load_wayback_config(config_path)
    gdelt_config = load_gdelt_config(config_path)
    timeout = float(os.getenv("PRESS_MONITOR_TIMEOUT_SECONDS", "20"))
    user_agent = os.getenv("PRESS_MONITOR_USER_AGENT", DEFAULT_USER_AGENT)
    headers = build_request_headers(user_agent)
    pause = float(os.getenv("PRESS_MONITOR_DOMAIN_PAUSE_SECONDS", "1.5"))
    results: list[DiscoveredURL] = []

    with httpx.Client(headers=headers, timeout=timeout, follow_redirects=True) as client:
        robots = RobotsCache(client, user_agent)
        with WaybackClient(
            timeout_seconds=wayback_config.discovery_timeout_seconds,
            request_delay_seconds=wayback_config.request_delay_seconds,
            max_retries=wayback_config.discovery_max_retries,
            headers=headers,
        ) as wayback_client:
            with GdeltClient(config=gdelt_config, headers=headers) as gdelt_client:
                for media in media_items:
                    LOGGER.info("Discovering %s (%s)", media.name, media.domain)
                    media_results = discover_media(media, target, client, robots, time_window=time_window)
                    if should_use_gdelt_discovery(media, gdelt_config.enabled):
                        media_results.extend(discover_media_gdelt(media, target, gdelt_client, gdelt_config.max_results_per_media, time_window=time_window))
                    if should_try_wayback_discovery(media, wayback_config, len(media_results)):
                        media_results.extend(discover_media_wayback_cdx(media, target, wayback_client, wayback_config, time_window=time_window))
                    results.extend(media_results)
                    time.sleep(pause)
    return results


def should_use_wayback_discovery(media: MediaConfig, config: WaybackConfig) -> bool:
    return bool(config.enabled and config.discovery_enabled and config.use_cdx and media.wayback_discovery)


def should_try_wayback_discovery(media: MediaConfig, config: WaybackConfig, candidate_count: int) -> bool:
    if not should_use_wayback_discovery(media, config):
        return False
    threshold = media.wayback_discovery_min_candidates
    if threshold is None:
        threshold = config.discovery_min_candidates
    return candidate_count < threshold


def should_use_gdelt_discovery(media: MediaConfig, enabled: bool) -> bool:
    return bool(enabled and media.gdelt_discovery)


def discover_media_gdelt(
    media: MediaConfig,
    target: date,
    client: GdeltClient,
    default_limit: int,
    time_window: tuple[datetime, datetime] | None = None,
) -> list[DiscoveredURL]:
    found: dict[str, DiscoveredURL] = {}
    start, end = _gdelt_discovery_window(target, time_window)
    limit = media.gdelt_discovery_limit or default_limit
    try:
        articles = client.find_articles(media.domain, start, end, limit=limit)
    except Exception as exc:
        LOGGER.info("GDELT discovery failed for %s: %s", media.domain, exc)
        return []
    for article in articles:
        _add_gdelt_discovered_article(found, media, article, target)
    return list(found.values())


def discover_media_wayback_cdx(
    media: MediaConfig,
    target: date,
    client: WaybackClient,
    config: WaybackConfig,
    time_window: tuple[datetime, datetime] | None = None,
) -> list[DiscoveredURL]:
    found: dict[str, DiscoveredURL] = {}
    start, end = _wayback_discovery_window(target, config, time_window)
    limit = media.wayback_discovery_limit or config.discovery_max_urls_per_media
    snapshots: list[WaybackSnapshot] = []
    if media.wayback_discovery_broad:
        try:
            snapshots.extend(client.find_domain_snapshots_cdx(media.domain, start, end, limit=limit))
        except Exception as exc:
            LOGGER.info("Wayback broad CDX discovery failed for %s: %s", media.domain, exc)
    for pattern in wayback_discovery_patterns(media, target):
        try:
            snapshots.extend(client.find_url_pattern_snapshots_cdx(pattern, start, end, limit=limit))
        except Exception as exc:
            LOGGER.info("Wayback CDX discovery pattern failed for %s (%s): %s", media.domain, pattern, exc)
            continue

    for snapshot in snapshots:
        if _snapshot_matches_discovery_target(snapshot, target):
            _add_wayback_discovered_snapshot(found, media, snapshot, target)
    return list(found.values())


def wayback_discovery_patterns(media: MediaConfig, target: date) -> list[str]:
    replacements = {
        "domain": media.domain,
        "year": f"{target.year:04d}",
        "month": f"{target.month:02d}",
        "day": f"{target.day:02d}",
        "yyyymmdd": f"{target.year:04d}{target.month:02d}{target.day:02d}",
    }
    configured = [
        template.format(**replacements)
        for template in media.wayback_discovery_patterns
    ]
    if configured:
        return configured
    return [
        f"www.{media.domain}/*/{replacements['year']}/{replacements['month']}/{replacements['day']}/*",
        f"{media.domain}/*/{replacements['year']}/{replacements['month']}/{replacements['day']}/*",
        f"*.{media.domain}/*/{replacements['year']}/{replacements['month']}/{replacements['day']}/*",
        f"www.{media.domain}/*{replacements['yyyymmdd']}*",
        f"{media.domain}/*{replacements['yyyymmdd']}*",
        f"*.{media.domain}/*{replacements['yyyymmdd']}*",
    ]


def entry_matches_target(
    entry: SitemapEntry,
    target: date,
    time_window: tuple[datetime, datetime] | None = None,
    allow_month_fallback: bool = False,
) -> bool:
    if time_window is not None:
        start, end = time_window
        dated_values = [entry.publication_date, entry.lastmod]
        parsed_values = [value for value in dated_values if parse_datetime(value)]
        if parsed_values:
            return any(datetime_in_window(value, start, end) for value in parsed_values)
        return False
    if datetime_matches_target(entry.publication_date, target):
        return True
    hinted = url_date_hint(entry.loc)
    if hinted == target:
        return True
    if allow_month_fallback and not _has_entry_level_date(entry) and url_month_hint(entry.loc) == (target.year, target.month):
        return True
    return datetime_matches_target(entry.lastmod, target)


def _discover_sitemap_url(
    media: MediaConfig,
    sitemap_url: str,
    discovered_from: str,
    target: date,
    client: httpx.Client,
    robots: RobotsCache,
    found: dict[str, DiscoveredURL],
    *,
    max_sitemaps: int,
    max_urls: int,
    seen_sitemaps: set[str] | None = None,
    source_type: str = "configured_sitemap",
    max_retries: int = 2,
    time_window: tuple[datetime, datetime] | None = None,
) -> None:
    seen_sitemaps = seen_sitemaps or set()
    if len(seen_sitemaps) >= max_sitemaps or sitemap_url in seen_sitemaps:
        return
    seen_sitemaps.add(sitemap_url)

    content = fetch_text(client, sitemap_url, retries=max_retries)
    kind, entries = parse_sitemap_xml(content)
    if kind == "sitemapindex":
        for entry in prioritize_sitemap_index_entries(entries, target)[:max_sitemaps]:
            if not robots.can_fetch(media.domain, entry.loc):
                continue
            _discover_sitemap_url(
                media,
                entry.loc,
                sitemap_url,
                target,
                client,
                robots,
                found,
                max_sitemaps=max_sitemaps,
                max_urls=max_urls,
                seen_sitemaps=seen_sitemaps,
                source_type=source_type,
                max_retries=max_retries,
                time_window=time_window,
            )
        return

    for entry in entries[:max_urls]:
        if entry_matches_target(entry, target, time_window=time_window, allow_month_fallback=media.allow_month_fallback):
            _add_discovered_entry(found, media, entry, discovered_from, target, source_type)


def _add_discovered_entry(
    found: dict[str, DiscoveredURL],
    media: MediaConfig,
    entry: SitemapEntry,
    discovered_from: str,
    target: date,
    source_type: str,
) -> None:
    filter_result = should_include_article_url(entry.loc, media)
    if not filter_result.included:
        return
    normalized = normalize_article_url(entry.loc)
    found[normalized] = DiscoveredURL(
        media_name=media.name,
        domain=media.domain,
        url=entry.loc,
        discovered_from=discovered_from,
        discovered_lastmod=entry.lastmod if source_type != "rss" else None,
        rss_published_at=(entry.publication_date or entry.lastmod) if source_type == "rss" else None,
        target_date=target.isoformat(),
        source_type=source_type,
        filter_status="included",
        filter_reason=filter_result.reason,
    )


def _add_wayback_discovered_snapshot(
    found: dict[str, DiscoveredURL],
    media: MediaConfig,
    snapshot: WaybackSnapshot,
    target: date,
) -> None:
    filter_result = should_include_article_url(snapshot.original_url, media)
    if not filter_result.included:
        return
    normalized = normalize_article_url(snapshot.original_url)
    if normalized in found:
        return
    capture_dt = timestamp_to_datetime(snapshot.timestamp).astimezone(MADRID_TZ)
    found[normalized] = DiscoveredURL(
        media_name=media.name,
        domain=media.domain,
        url=snapshot.original_url,
        discovered_from=f"wayback_cdx:{snapshot.timestamp}",
        discovered_lastmod=capture_dt.isoformat(),
        rss_published_at=None,
        target_date=target.isoformat(),
        source_type="wayback_cdx_discovery",
        filter_status="included",
        filter_reason=filter_result.reason,
    )


def _add_gdelt_discovered_article(
    found: dict[str, DiscoveredURL],
    media: MediaConfig,
    article: GdeltArticle,
    target: date,
) -> None:
    filter_result = should_include_article_url(article.url, media)
    if not filter_result.included:
        return
    normalized = normalize_article_url(article.url)
    if normalized in found:
        return
    found[normalized] = DiscoveredURL(
        media_name=media.name,
        domain=media.domain,
        url=article.url,
        discovered_from="gdelt",
        discovered_lastmod=article.seendate,
        rss_published_at=None,
        target_date=target.isoformat(),
        source_type="gdelt",
        filter_status="included",
        filter_reason=filter_result.reason,
    )


def backoff_delay(attempt: int) -> float:
    return min(2**attempt, 30) + random.uniform(0, 0.5)


def _is_retryable_fetch_error(exc: Exception) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        return status == 429 or status >= 500
    return True


def prioritize_sitemap_index_entries(entries: list[SitemapEntry], target: date) -> list[SitemapEntry]:
    indexed = list(enumerate(entries))
    return [
        entry
        for _index, entry in sorted(
            indexed,
            key=lambda item: (_sitemap_index_priority(item[1], target), item[0]),
        )
    ]


def _sitemap_index_priority(entry: SitemapEntry, target: date) -> int:
    loc = entry.loc.lower()
    month_tokens = (
        f"{target.year}{target.month:02d}",
        f"{target.year}-{target.month:02d}",
        f"{target.year}_{target.month:02d}",
        f"/{target.year}/{target.month:02d}",
    )
    if any(token in loc for token in month_tokens):
        return 0
    lastmod = parse_datetime(entry.lastmod)
    if lastmod and lastmod.year == target.year and lastmod.month == target.month:
        return 1
    if str(target.year) in loc:
        return 2
    if "news" in loc or "noticias" in loc or "google" in loc:
        return 3
    return 4


def _has_entry_level_date(entry: SitemapEntry) -> bool:
    return bool(parse_datetime(entry.publication_date) or url_date_hint(entry.loc))


def _wayback_discovery_window(
    target: date,
    config: WaybackConfig,
    time_window: tuple[datetime, datetime] | None,
) -> tuple[datetime, datetime]:
    if time_window is not None:
        return time_window
    start, end = start_end_for_date(target)
    start = start - timedelta(days=config.discovery_max_days_before)
    end = end + timedelta(days=config.discovery_max_days_after)
    return start, end


def _gdelt_discovery_window(
    target: date,
    time_window: tuple[datetime, datetime] | None,
) -> tuple[datetime, datetime]:
    if time_window is not None:
        return time_window
    return start_end_for_date(target)


def _snapshot_matches_discovery_target(snapshot: WaybackSnapshot, target: date) -> bool:
    hinted_day = url_date_hint(snapshot.original_url)
    if hinted_day is not None:
        return hinted_day == target
    hinted_month = url_month_hint(snapshot.original_url)
    if hinted_month is not None and hinted_month != (target.year, target.month):
        return False
    return True


def _robots_sitemaps(text: str) -> list[str]:
    sitemaps: list[str] = []
    for line in text.splitlines():
        if line.lower().startswith("sitemap:"):
            value = line.split(":", 1)[1].strip()
            if value:
                sitemaps.append(value)
    return sitemaps


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _load_page_range(value: object) -> tuple[int, int] | None:
    if value is None:
        return None
    if isinstance(value, list) and len(value) == 2:
        start, end = int(value[0]), int(value[1])
        if end < start:
            raise ValueError("sitemap_page_range end must be >= start")
        return start, end
    raise ValueError("sitemap_page_range must be a two-item list, e.g. [0, 5]")


def _optional_float(value: object) -> float | None:
    return None if value is None else float(value)


def _optional_int(value: object) -> int | None:
    return None if value is None else int(value)


def _child_text(node: ET.Element, child_name: str) -> str | None:
    for child in node:
        if _local_name(child.tag) == child_name and child.text:
            return child.text.strip()
    return None


def _descendant_text(node: ET.Element, child_name: str) -> str | None:
    for child in node.iter():
        if _local_name(child.tag) == child_name and child.text:
            return child.text.strip()
    return None
