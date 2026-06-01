from __future__ import annotations

import json
import logging
import os
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.robotparser import RobotFileParser

import httpx
import trafilatura
from lxml import html as lxml_html

from src.models import ExtractedArticle, WaybackConfig
from src.utils import (
    DEFAULT_USER_AGENT,
    MADRID_TZ,
    build_request_headers,
    count_words,
    normalize_article_url,
    normalize_text_whitespace,
    parse_datetime,
    parse_target_date,
    text_sha256,
)
from src.wayback_client import (
    WaybackClient,
    WaybackSnapshot,
    build_wayback_replay_urls,
    find_best_snapshot,
    load_wayback_config,
)

LOGGER = logging.getLogger(__name__)
PAYWALL_HINTS = (
    "suscríbete",
    "suscribete",
    "solo para suscriptores",
    "contenido exclusivo",
    "subscription",
    "paywall",
    "regístrate para seguir leyendo",
    "registrate para seguir leyendo",
    "exclusive content",
    '"isaccessibleforfree": false',
)
PAYWALL_HINTS = (
    "suscr\u00edbete",
    "suscribete",
    "solo para suscriptores",
    "contenido exclusivo",
    "subscription",
    "paywall",
    "reg\u00edstrate para seguir leyendo",
    "registrate para seguir leyendo",
    "exclusive content",
)
PAYWALL_SCHEMA_HINTS = ('"isaccessibleforfree":false', '"isaccessibleforfree": false')
PAYWALL_COMPLETENESS_MIN_WORDS = 300
WAYBACK_RECOVERABLE_STATUSES = {
    "no_text",
    "too_short",
    "paywall_or_incomplete",
    "parse_error",
    "wayback_fetch_error",
}
WAYBACK_HTTP_CODES = {"403", "404", "410", "429", "500", "502", "503"}


@dataclass
class DomainRateLimiter:
    pause_seconds: float
    max_concurrency_per_domain: int = 1
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _last_request: dict[str, float] = field(default_factory=dict)
    _semaphores: dict[str, threading.Semaphore] = field(default_factory=dict)

    def semaphore(self, domain: str) -> threading.Semaphore:
        with self._lock:
            if domain not in self._semaphores:
                self._semaphores[domain] = threading.Semaphore(self.max_concurrency_per_domain)
            return self._semaphores[domain]

    def wait(self, domain: str) -> None:
        with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_request.get(domain, 0.0)
            remaining = self.pause_seconds - elapsed
            if remaining > 0:
                time.sleep(remaining)
            self._last_request[domain] = time.monotonic()


class ArticleRobotsCache:
    def __init__(self, client: httpx.Client, user_agent: str) -> None:
        self.client = client
        self.user_agent = user_agent
        self._cache: dict[str, RobotFileParser] = {}
        self._lock = threading.Lock()

    def can_fetch(self, domain: str, url: str) -> bool:
        if os.getenv("PRESS_MONITOR_RESPECT_ROBOTS", "true").lower() not in {"1", "true", "yes"}:
            return True
        with self._lock:
            parser = self._cache.get(domain)
            if parser is None:
                parser = RobotFileParser()
                parser.set_url(f"https://{domain}/robots.txt")
                try:
                    response = self.client.get(f"https://{domain}/robots.txt")
                    parser.parse(response.text.splitlines() if response.status_code < 400 else [])
                except httpx.HTTPError:
                    parser.parse([])
                self._cache[domain] = parser
        return parser.can_fetch(self.user_agent, url)


def fetch_html(client: httpx.Client, url: str, retries: int = 2) -> str:
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            response = client.get(url)
            response.raise_for_status()
            return response.text
        except httpx.HTTPError as exc:
            last_exc = exc
            if attempt < retries:
                delay = min(2**attempt, 30) + random.uniform(0, 0.5)
                LOGGER.info("Retrying %s in %.2fs after %s", url, delay, exc)
                time.sleep(delay)
    assert last_exc is not None
    raise last_exc


def _trafilatura_extract_data(url: str, html: str, *, favor_precision: bool) -> dict[str, Any]:
    raw = trafilatura.extract(
        html,
        url=url,
        output_format="json",
        with_metadata=True,
        include_comments=False,
        include_tables=False,
        favor_precision=favor_precision,
        favor_recall=not favor_precision,
    )
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {"text": raw}
    return data if isinstance(data, dict) else {}


def extract_article_from_html(
    url: str,
    html: str,
    target_date: str,
    min_words: int = 150,
    *,
    content_source: str = "live",
    source_url: str | None = None,
    original_url: str | None = None,
    wayback_snapshot: WaybackSnapshot | None = None,
) -> ExtractedArticle:
    normalized_url = normalize_article_url(url)
    try:
        metadata = extract_metadata(url, html)
        data = _trafilatura_extract_data(url, html, favor_precision=True)
        text = normalize_text_whitespace(data.get("text") or "")
        words = count_words(text)
        if words < min_words:
            recall_data = _trafilatura_extract_data(url, html, favor_precision=False)
            recall_text = normalize_text_whitespace(recall_data.get("text") or "")
            if count_words(recall_text) > words:
                data = recall_data
    except Exception as exc:
        status = "wayback_parse_error" if content_source == "wayback" else "parse_error"
        return _empty_article(
            url,
            normalized_url,
            target_date,
            status,
            str(exc),
            content_source=content_source,
            source_url=source_url,
            original_url=original_url,
            wayback_snapshot=wayback_snapshot,
        )

    text = normalize_text_whitespace(data.get("text") or "")
    words = count_words(text)
    title = metadata.get("title") or data.get("title")
    author = metadata.get("author") or data.get("author")
    published_at = _datetime_iso(metadata.get("article_published_at") or data.get("date") or data.get("published"))
    modified_at = _datetime_iso(metadata.get("article_modified_at"))
    metadata_paywall = bool(metadata.get("is_paywalled"))
    paywall_hint = looks_like_paywall(html, text)
    is_paywalled = metadata_paywall or paywall_hint

    status = "ok_wayback" if content_source == "wayback" else "ok_live"
    error = None
    if not text:
        status = "paywall_or_incomplete" if is_paywalled else "no_text"
        error = "No article text extracted"
    elif words < min_words:
        status = "paywall_or_incomplete" if is_paywalled else ("wayback_too_short" if content_source == "wayback" else "too_short")
        error = "Paywall or incomplete article suspected" if is_paywalled else f"Only {words} words extracted; minimum is {min_words}"
    elif is_paywalled and words < max(min_words, PAYWALL_COMPLETENESS_MIN_WORDS):
        status = "paywall_or_incomplete"
        error = "Paywall or incomplete article suspected"
    published_dt = parse_datetime(published_at)
    if status in {"ok_live", "ok_wayback"} and published_dt and published_dt.date() != parse_target_date(target_date):
        status = "out_of_target_date"
        error = f"Article published at {published_dt.date().isoformat()}, outside target date {target_date}"

    return ExtractedArticle(
        url=url,
        normalized_url=normalized_url,
        target_date=target_date,
        title=title,
        author=author,
        article_published_at=published_at,
        article_modified_at=modified_at,
        section=metadata.get("section"),
        tags=list(metadata.get("tags") or []),
        canonical_url=metadata.get("canonical_url"),
        language=metadata.get("language"),
        is_paywalled=is_paywalled,
        text_clean=text or None,
        word_count=words,
        text_hash=text_sha256(text) if text else None,
        extraction_status=status,
        error=error,
        content_source=content_source,
        source_url=source_url or url,
        original_url=original_url or url,
        wayback_timestamp=wayback_snapshot.timestamp if wayback_snapshot else None,
        wayback_distance_seconds=wayback_snapshot.distance_seconds if wayback_snapshot else None,
        wayback_statuscode=str(wayback_snapshot.statuscode) if wayback_snapshot and wayback_snapshot.statuscode is not None else None,
        wayback_mimetype=wayback_snapshot.mimetype if wayback_snapshot else None,
        wayback_digest=wayback_snapshot.digest if wayback_snapshot else None,
        wayback_source_api=wayback_snapshot.source_api if wayback_snapshot else None,
    )


def extract_url(
    row: Any,
    client: httpx.Client,
    robots: ArticleRobotsCache,
    limiter: DomainRateLimiter,
    min_words: int,
    wayback_mode: str,
    wayback_config: WaybackConfig,
    wayback_client: WaybackClient | None,
) -> ExtractedArticle:
    url = row["url"]
    domain = row["domain"]
    target_date = row["target_date"]
    normalized_url = normalize_article_url(url)
    if not robots.can_fetch(domain, url):
        article = _empty_article(url, normalized_url, target_date, "http_error", "Blocked by robots.txt")
        return _maybe_extract_wayback(row, article, min_words, wayback_mode, wayback_config, wayback_client)

    try:
        with limiter.semaphore(domain):
            limiter.wait(domain)
            html = fetch_html(client, url, retries=int(os.getenv("PRESS_MONITOR_MAX_RETRIES", "2")))
        article = extract_article_from_html(url, html, target_date, min_words=min_words)
    except httpx.HTTPError as exc:
        LOGGER.warning("HTTP extraction failed for %s: %s", url, exc)
        article = _empty_article(url, normalized_url, target_date, "http_error", str(exc))
    except Exception as exc:
        LOGGER.warning("Extraction failed for %s: %s", url, exc)
        article = _empty_article(url, normalized_url, target_date, "parse_error", str(exc))
    return _maybe_extract_wayback(row, article, min_words, wayback_mode, wayback_config, wayback_client)


def iter_extract_many(rows: list[Any], *, wayback_mode: str = "fallback", config_path: str = "config/media.yaml"):
    user_agent = os.getenv("PRESS_MONITOR_USER_AGENT", DEFAULT_USER_AGENT)
    timeout = float(os.getenv("PRESS_MONITOR_TIMEOUT_SECONDS", "20"))
    min_words = int(os.getenv("PRESS_MONITOR_MIN_WORDS", "150"))
    max_workers = max(1, int(os.getenv("PRESS_MONITOR_EXTRACT_CONCURRENCY", "3")))
    pause = float(os.getenv("PRESS_MONITOR_DOMAIN_PAUSE_SECONDS", "1.5"))
    per_domain = max(1, int(os.getenv("PRESS_MONITOR_MAX_CONCURRENCY_PER_DOMAIN", "1")))

    headers = build_request_headers(user_agent)
    limiter = DomainRateLimiter(pause_seconds=pause, max_concurrency_per_domain=per_domain)
    wayback_config = load_wayback_config(config_path)
    if wayback_mode == "off":
        wayback_config = WaybackConfig(enabled=False)
    elif wayback_mode == "always":
        wayback_config = WaybackConfig(
            enabled=wayback_config.enabled,
            strategy="always_check",
            max_days_after=wayback_config.max_days_after,
            max_days_before=wayback_config.max_days_before,
            request_delay_seconds=wayback_config.request_delay_seconds,
            max_retries=wayback_config.max_retries,
            timeout_seconds=wayback_config.timeout_seconds,
            use_cdx=wayback_config.use_cdx,
            use_availability=wayback_config.use_availability,
            extended_search=wayback_config.extended_search,
            extended_max_days_after=wayback_config.extended_max_days_after,
            extended_max_days_before=wayback_config.extended_max_days_before,
        )
    wayback_client_ctx = WaybackClient(
        timeout_seconds=wayback_config.timeout_seconds,
        request_delay_seconds=wayback_config.request_delay_seconds,
        max_retries=wayback_config.max_retries,
        headers=headers,
    ) if wayback_config.enabled else None
    with httpx.Client(headers=headers, timeout=timeout, follow_redirects=True) as client:
        robots = ArticleRobotsCache(client, user_agent)
        with (wayback_client_ctx or _NullWaybackClient()) as wb_client:
            if max_workers == 1:
                for row in rows:
                    yield row, extract_url(row, client, robots, limiter, min_words, wayback_mode, wayback_config, wb_client)
                return

            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_map = {
                    executor.submit(extract_url, row, client, robots, limiter, min_words, wayback_mode, wayback_config, wb_client): row
                    for row in rows
                }
                for future in as_completed(future_map):
                    row = future_map[future]
                    yield row, future.result()


def extract_many(rows: list[Any], *, wayback_mode: str = "fallback", config_path: str = "config/media.yaml") -> list[tuple[Any, ExtractedArticle]]:
    return list(iter_extract_many(rows, wayback_mode=wayback_mode, config_path=config_path))


def looks_like_paywall(html: str, text: str | None = None) -> bool:
    compact_html = re.sub(r"\s+", "", html.lower())
    if any(hint in compact_html for hint in PAYWALL_SCHEMA_HINTS):
        return True
    haystack = text if text and count_words(text) >= 80 else _visible_content_text(html)
    if not haystack:
        haystack = html
    haystack = haystack.lower()
    return any(hint in haystack for hint in PAYWALL_HINTS)


def extract_metadata(url: str, html: str) -> dict[str, Any]:
    doc = lxml_html.fromstring(html)
    metadata: dict[str, Any] = {}
    json_ld = _first_article_json_ld(doc)
    if json_ld:
        metadata.update(_metadata_from_json_ld(json_ld))

    metadata.setdefault("title", _meta(doc, "og:title") or _text_first(doc, "//title/text()") or _text_first(doc, "//h1//text()"))
    metadata.setdefault("author", _meta(doc, "author") or _meta(doc, "article:author"))
    metadata.setdefault(
        "article_published_at",
        _meta(doc, "article:published_time") or _meta(doc, "datePublished") or _meta(doc, "date"),
    )
    metadata.setdefault("article_modified_at", _meta(doc, "article:modified_time") or _meta(doc, "dateModified"))
    metadata.setdefault("section", _meta(doc, "article:section") or _meta(doc, "section"))
    metadata.setdefault("canonical_url", _attr_first(doc, "//link[translate(@rel, 'CANONICAL', 'canonical')='canonical']/@href") or url)
    metadata.setdefault("language", doc.get("lang") or _attr_first(doc, "//html/@lang"))
    if "tags" not in metadata:
        metadata["tags"] = _meta_all(doc, "article:tag") or _keywords(doc)
    if not metadata.get("article_published_at"):
        metadata["article_published_at"] = _attr_first(doc, "//time/@datetime")
    metadata["is_paywalled"] = bool(metadata.get("is_paywalled")) or looks_like_paywall(html)
    return metadata


def _first_article_json_ld(doc: Any) -> dict[str, Any] | None:
    for script in doc.xpath("//script[@type='application/ld+json']/text()"):
        for item in _json_ld_items(script):
            item_type = item.get("@type")
            types = item_type if isinstance(item_type, list) else [item_type]
            if any(t in {"NewsArticle", "Article", "ReportageNewsArticle", "OpinionNewsArticle"} for t in types):
                return item
    return None


def _json_ld_items(raw: str) -> list[dict[str, Any]]:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return []
    candidates = parsed if isinstance(parsed, list) else [parsed]
    items: list[dict[str, Any]] = []
    for candidate in candidates:
        if isinstance(candidate, dict):
            graph = candidate.get("@graph")
            if isinstance(graph, list):
                items.extend(item for item in graph if isinstance(item, dict))
            items.append(candidate)
    return items


def _metadata_from_json_ld(item: dict[str, Any]) -> dict[str, Any]:
    main_entity = item.get("mainEntityOfPage")
    canonical = main_entity.get("@id") if isinstance(main_entity, dict) else item.get("url")
    return {
        "title": item.get("headline") or item.get("name"),
        "author": _author_name(item.get("author")),
        "article_published_at": item.get("datePublished"),
        "article_modified_at": item.get("dateModified"),
        "section": item.get("articleSection"),
        "tags": _keywords_from_json_ld(item.get("keywords")),
        "canonical_url": canonical,
        "language": item.get("inLanguage"),
        "is_paywalled": item.get("isAccessibleForFree") is False,
    }


def _author_name(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return value.get("name")
    if isinstance(value, list):
        names = [_author_name(item) for item in value]
        return ", ".join(name for name in names if name)
    return None


def _keywords_from_json_ld(value: Any) -> list[str]:
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, list):
        return [str(part).strip() for part in value if str(part).strip()]
    return []


def _meta(doc: Any, name: str) -> str | None:
    return _attr_first(
        doc,
        f"//meta[translate(@property, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz')='{name.lower()}' "
        f"or translate(@name, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz')='{name.lower()}']/@content",
    )


def _meta_all(doc: Any, name: str) -> list[str]:
    values = doc.xpath(
        f"//meta[translate(@property, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz')='{name.lower()}' "
        f"or translate(@name, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz')='{name.lower()}']/@content"
    )
    return [str(value).strip() for value in values if str(value).strip()]


def _keywords(doc: Any) -> list[str]:
    value = _meta(doc, "keywords")
    return [part.strip() for part in value.split(",") if part.strip()] if value else []


def _attr_first(doc: Any, xpath: str) -> str | None:
    values = doc.xpath(xpath)
    if not values:
        return None
    value = str(values[0]).strip()
    return value or None


def _text_first(doc: Any, xpath: str) -> str | None:
    values = [str(value).strip() for value in doc.xpath(xpath) if str(value).strip()]
    return normalize_text_whitespace(" ".join(values)) if values else None


def _datetime_iso(value: str | None) -> str | None:
    parsed = parse_datetime(value)
    return parsed.isoformat() if parsed else value


def _maybe_extract_wayback(
    row: Any,
    live_article: ExtractedArticle,
    min_words: int,
    wayback_mode: str,
    config: WaybackConfig,
    client: WaybackClient | None,
) -> ExtractedArticle:
    if not config.enabled or client is None:
        return live_article
    if wayback_mode == "off":
        return live_article
    if wayback_mode != "always" and not should_try_wayback(live_article):
        return live_article
    url = row["url"]
    target_datetime = _target_datetime_for_row(row)
    try:
        snapshot = find_best_snapshot(client, url, target_datetime, config)
    except Exception as exc:
        if should_try_wayback(live_article) or wayback_mode == "always":
            return _empty_article(
                url,
                live_article.normalized_url,
                live_article.target_date,
                "wayback_fetch_error",
                str(exc),
                content_source="wayback",
                original_url=url,
            )
        return live_article
    live_needs_recovery = should_try_wayback(live_article)
    if not snapshot:
        if live_needs_recovery:
            return _empty_article(
                url,
                live_article.normalized_url,
                live_article.target_date,
                "wayback_not_found",
                "No suitable Wayback snapshot found",
                content_source="wayback",
                original_url=url,
            )
        return live_article
    last_error: Exception | None = None
    last_url: str | None = None
    for replay_url in build_wayback_replay_urls(snapshot):
        last_url = replay_url
        try:
            if client._client is not None:
                http_client = client._client
                html = fetch_html(http_client, replay_url, retries=config.max_retries)
            else:
                with httpx.Client(
                    headers=client.headers,
                    timeout=client.timeout_seconds,
                    follow_redirects=True,
                ) as http_client:
                    html = fetch_html(http_client, replay_url, retries=config.max_retries)
            return extract_article_from_html(
                url,
                html,
                live_article.target_date,
                min_words=min_words,
                content_source="wayback",
                source_url=replay_url,
                original_url=url,
                wayback_snapshot=snapshot,
            )
        except httpx.HTTPError as exc:
            last_error = exc
            LOGGER.info("Wayback replay failed for %s via %s: %s", url, replay_url, exc)
            continue
    if last_error is not None:
        if not live_needs_recovery:
            return live_article
        return _empty_article(
            last_url or url,
            live_article.normalized_url,
            live_article.target_date,
            "wayback_fetch_error",
            str(last_error),
            content_source="wayback",
            source_url=last_url,
            original_url=url,
            wayback_snapshot=snapshot,
        )
    return live_article


def should_try_wayback(article: ExtractedArticle) -> bool:
    if article.extraction_status in WAYBACK_RECOVERABLE_STATUSES:
        return True
    if article.extraction_status == "http_error" and article.error:
        lowered = article.error.lower()
        status_codes = set(re.findall(r"\b(?:403|404|410|429|500|502|503)\b", article.error))
        return bool(status_codes & WAYBACK_HTTP_CODES) or "timeout" in lowered or "timed out" in lowered
    return False


def _target_datetime_for_row(row: Any) -> Any:
    for key in ("rss_published_at", "discovered_lastmod", "lastmod"):
        try:
            value = row[key]
        except (KeyError, IndexError):
            value = None
        parsed = parse_datetime(value)
        if parsed:
            return parsed
    target = parse_target_date(row["target_date"])
    return datetime.combine(target, time_module_noon(), tzinfo=MADRID_TZ)


def time_module_noon():
    from datetime import time as datetime_time

    return datetime_time(12, 0)


def _visible_content_text(html: str) -> str:
    try:
        doc = lxml_html.fromstring(html)
    except Exception:
        return normalize_text_whitespace(html)
    for node in doc.xpath("//script|//style|//noscript|//nav|//header|//footer|//aside|//form|//button"):
        parent = node.getparent()
        if parent is not None:
            parent.remove(node)
    values = doc.xpath("//article//text()")
    if not values:
        values = doc.xpath("//main//text()")
    if not values:
        values = doc.xpath("//body//text()")
    return normalize_text_whitespace(" ".join(str(value) for value in values))


class _NullWaybackClient:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *args: object) -> None:
        return None


def _empty_article(
    url: str,
    normalized_url: str,
    target_date: str,
    status: str,
    error: str | None,
    *,
    content_source: str = "live",
    source_url: str | None = None,
    original_url: str | None = None,
    wayback_snapshot: WaybackSnapshot | None = None,
) -> ExtractedArticle:
    return ExtractedArticle(
        url=url,
        normalized_url=normalized_url,
        target_date=target_date,
        title=None,
        author=None,
        article_published_at=None,
        article_modified_at=None,
        section=None,
        tags=[],
        canonical_url=None,
        language=None,
        is_paywalled=False,
        text_clean=None,
        word_count=0,
        text_hash=None,
        extraction_status=status,
        error=error,
        content_source=content_source,
        source_url=source_url or url,
        original_url=original_url or url,
        wayback_timestamp=wayback_snapshot.timestamp if wayback_snapshot else None,
        wayback_distance_seconds=wayback_snapshot.distance_seconds if wayback_snapshot else None,
        wayback_statuscode=str(wayback_snapshot.statuscode) if wayback_snapshot and wayback_snapshot.statuscode is not None else None,
        wayback_mimetype=wayback_snapshot.mimetype if wayback_snapshot else None,
        wayback_digest=wayback_snapshot.digest if wayback_snapshot else None,
        wayback_source_api=wayback_snapshot.source_api if wayback_snapshot else None,
    )
