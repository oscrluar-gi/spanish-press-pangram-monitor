from __future__ import annotations

import logging
import os
import random
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

import httpx
import yaml

from src.models import GdeltConfig
from src.utils import MADRID_TZ, build_request_headers

LOGGER = logging.getLogger(__name__)
GDELT_DOC_URL = "https://api.gdeltproject.org/api/v2/doc/doc"


@dataclass(frozen=True)
class GdeltArticle:
    url: str
    title: str | None = None
    seendate: str | None = None
    source_country: str | None = None


class GdeltClient:
    def __init__(
        self,
        *,
        config: GdeltConfig | None = None,
        client: httpx.Client | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.config = config or GdeltConfig()
        self._client = client
        self.headers = headers or build_request_headers()
        self._last_request = 0.0
        self._cooldown_until = 0.0
        self._cache: dict[tuple[tuple[str, str], ...], Any] = {}
        self._temporary_failures: dict[tuple[tuple[str, str], ...], tuple[float, str]] = {}
        self._lock = threading.Lock()

    def __enter__(self) -> "GdeltClient":
        if self._client is None:
            self._client = httpx.Client(
                timeout=self.config.timeout_seconds,
                follow_redirects=True,
                headers=self.headers,
            )
        return self

    def __exit__(self, *args: object) -> None:
        if self._client is not None:
            self._client.close()

    def find_articles(
        self,
        domain: str,
        start_datetime: datetime,
        end_datetime: datetime,
        *,
        limit: int,
    ) -> list[GdeltArticle]:
        rows: list[GdeltArticle] = []
        errors: list[str] = []
        for query in (f"domainis:{domain}", f"domain:{domain}"):
            params = {
                "query": query,
                "mode": "ArtList",
                "format": "json",
                "maxrecords": str(limit),
                "sort": "DateDesc",
                "startdatetime": format_gdelt_datetime(start_datetime),
                "enddatetime": format_gdelt_datetime(end_datetime),
            }
            try:
                payload = self._get_json(params)
            except Exception as exc:
                errors.append(f"{query}: {exc}")
                LOGGER.info("GDELT failed for %s: %s", query, exc)
                continue
            rows.extend(
                article
                for article in parse_gdelt_articles(payload)
                if url_matches_domain(article.url, domain)
            )
            if rows:
                return dedupe_articles(rows)[:limit]
        if errors:
            LOGGER.info("GDELT returned no articles for %s; errors=%s", domain, "; ".join(errors))
        return []

    def _get_json(self, params: dict[str, str]) -> Any:
        assert self._client is not None
        cache_key = tuple(sorted(params.items()))
        cached = self._cache.get(cache_key)
        if cached is not None:
            LOGGER.debug("Using cached GDELT response for %s", params.get("query"))
            return cached
        self._raise_if_temporarily_blocked(cache_key)

        last_exc: Exception | None = None
        for attempt in range(self.config.max_retries + 1):
            try:
                self._rate_limit()
                response = self._client.get(GDELT_DOC_URL, params=params)
                if response.status_code == 429:
                    self._activate_cooldown(cache_key, "HTTP 429")
                    if self.config.cooldown_seconds > 0:
                        raise RuntimeError(f"GDELT rate-limited; cooldown active for {self.config.cooldown_seconds:.0f}s")
                    if attempt < self.config.max_retries:
                        delay = self._retry_delay(attempt)
                        LOGGER.info("Retrying GDELT in %.2fs after HTTP 429", delay)
                        time.sleep(delay)
                        continue
                if response.status_code >= 500:
                    if attempt < self.config.max_retries:
                        delay = self._retry_delay(attempt)
                        LOGGER.info("Retrying GDELT in %.2fs after HTTP %s", delay, response.status_code)
                        time.sleep(delay)
                        continue
                response.raise_for_status()
                payload = response.json()
                self._cache[cache_key] = payload
                return payload
            except Exception as exc:
                last_exc = exc
                self._raise_if_temporarily_blocked(cache_key)
                if attempt < self.config.max_retries:
                    delay = self._retry_delay(attempt)
                    LOGGER.info("Retrying GDELT in %.2fs after %s", delay, exc)
                    time.sleep(delay)
                    continue
                break
        raise RuntimeError(str(last_exc))

    def _rate_limit(self) -> None:
        with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_request
            remaining = self.config.request_delay_seconds - elapsed
            if remaining > 0:
                time.sleep(remaining)
            self._last_request = time.monotonic()

    def _retry_delay(self, attempt: int) -> float:
        base = self.config.retry_delay_seconds * (2**attempt)
        delay = min(base, self.config.max_retry_delay_seconds)
        if self.config.retry_jitter_seconds > 0:
            delay += random.uniform(0, self.config.retry_jitter_seconds)
        return delay

    def _activate_cooldown(self, cache_key: tuple[tuple[str, str], ...], reason: str) -> None:
        if self.config.cooldown_seconds <= 0:
            return
        until = time.monotonic() + self.config.cooldown_seconds
        with self._lock:
            self._cooldown_until = max(self._cooldown_until, until)
            self._temporary_failures[cache_key] = (self._cooldown_until, reason)
        LOGGER.info("GDELT cooldown active for %.0fs after %s", self.config.cooldown_seconds, reason)

    def _raise_if_temporarily_blocked(self, cache_key: tuple[tuple[str, str], ...]) -> None:
        now = time.monotonic()
        with self._lock:
            if now < self._cooldown_until:
                remaining = self._cooldown_until - now
                failure = self._temporary_failures.get(cache_key)
                if failure and now >= failure[0]:
                    self._temporary_failures.pop(cache_key, None)
                raise RuntimeError(f"GDELT cooldown active for {remaining:.0f}s")
            failure = self._temporary_failures.get(cache_key)
            if failure and now < failure[0]:
                remaining = failure[0] - now
                raise RuntimeError(f"GDELT temporary failure cached for {remaining:.0f}s: {failure[1]}")
            if failure:
                self._temporary_failures.pop(cache_key, None)


def load_gdelt_config(path: str = "config/media.yaml") -> GdeltConfig:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle) or {}
    except FileNotFoundError:
        raw = {}
    data = raw.get("gdelt", {})
    return GdeltConfig(
        enabled=_bool_env("GDELT_ENABLED", data.get("gdelt_enabled", data.get("enabled", True))),
        max_results_per_media=int(os.getenv("GDELT_MAX_RESULTS_PER_MEDIA") or data.get("gdelt_max_results_per_media", data.get("max_results_per_media", 25))),
        request_delay_seconds=float(os.getenv("GDELT_REQUEST_DELAY_SECONDS") or data.get("gdelt_request_delay_seconds", data.get("request_delay_seconds", 12.0))),
        max_retries=int(os.getenv("GDELT_MAX_RETRIES") or data.get("gdelt_max_retries", data.get("max_retries", 2))),
        retry_delay_seconds=float(os.getenv("GDELT_RETRY_DELAY_SECONDS") or data.get("gdelt_retry_delay_seconds", data.get("retry_delay_seconds", 15.0))),
        max_retry_delay_seconds=float(os.getenv("GDELT_MAX_RETRY_DELAY_SECONDS") or data.get("gdelt_max_retry_delay_seconds", data.get("max_retry_delay_seconds", 120.0))),
        retry_jitter_seconds=float(os.getenv("GDELT_RETRY_JITTER_SECONDS") or data.get("gdelt_retry_jitter_seconds", data.get("retry_jitter_seconds", 3.0))),
        cooldown_seconds=float(os.getenv("GDELT_COOLDOWN_SECONDS") or data.get("gdelt_cooldown_seconds", data.get("cooldown_seconds", 300.0))),
        discovery_min_candidates=int(os.getenv("GDELT_DISCOVERY_MIN_CANDIDATES") or data.get("gdelt_discovery_min_candidates", data.get("discovery_min_candidates", 5))),
        timeout_seconds=float(os.getenv("GDELT_TIMEOUT_SECONDS") or data.get("gdelt_timeout_seconds", data.get("timeout_seconds", 30.0))),
    )


def parse_gdelt_articles(payload: Any) -> list[GdeltArticle]:
    if not isinstance(payload, dict):
        return []
    articles = payload.get("articles")
    if not isinstance(articles, list):
        return []
    result: list[GdeltArticle] = []
    for item in articles:
        if not isinstance(item, dict):
            continue
        url = item.get("url")
        if not url:
            continue
        result.append(
            GdeltArticle(
                url=str(url),
                title=str(item["title"]) if item.get("title") is not None else None,
                seendate=str(item["seendate"]) if item.get("seendate") is not None else None,
                source_country=str(item["sourceCountry"]) if item.get("sourceCountry") is not None else None,
            )
        )
    return result


def dedupe_articles(articles: list[GdeltArticle]) -> list[GdeltArticle]:
    seen: set[str] = set()
    result: list[GdeltArticle] = []
    for article in articles:
        key = article.url
        if key in seen:
            continue
        seen.add(key)
        result.append(article)
    return result


def format_gdelt_datetime(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=MADRID_TZ)
    return value.astimezone(timezone.utc).strftime("%Y%m%d%H%M%S")


def url_matches_domain(url: str, domain: str) -> bool:
    host = urlsplit(url).netloc.lower()
    domain = domain.lower()
    return host == domain or host.endswith(f".{domain}")


def _bool_env(name: str, default: object) -> bool:
    value = os.getenv(name)
    if value is None:
        return bool(default)
    return value.lower() in {"1", "true", "yes", "on"}
