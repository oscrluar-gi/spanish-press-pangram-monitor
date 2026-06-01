from __future__ import annotations

import logging
import os
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
        last_exc: Exception | None = None
        for attempt in range(self.config.max_retries + 1):
            try:
                self._rate_limit()
                response = self._client.get(GDELT_DOC_URL, params=params)
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt < self.config.max_retries:
                        LOGGER.info("Retrying GDELT in %.2fs after HTTP %s", self.config.retry_delay_seconds, response.status_code)
                        time.sleep(self.config.retry_delay_seconds)
                        continue
                response.raise_for_status()
                return response.json()
            except Exception as exc:
                last_exc = exc
                if attempt < self.config.max_retries:
                    LOGGER.info("Retrying GDELT in %.2fs after %s", self.config.retry_delay_seconds, exc)
                    time.sleep(self.config.retry_delay_seconds)
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


def load_gdelt_config(path: str = "config/media.yaml") -> GdeltConfig:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle) or {}
    except FileNotFoundError:
        raw = {}
    data = raw.get("gdelt", {})
    return GdeltConfig(
        enabled=_bool_env("GDELT_ENABLED", data.get("gdelt_enabled", data.get("enabled", True))),
        max_results_per_media=int(os.getenv("GDELT_MAX_RESULTS_PER_MEDIA") or data.get("gdelt_max_results_per_media", data.get("max_results_per_media", 100))),
        request_delay_seconds=float(os.getenv("GDELT_REQUEST_DELAY_SECONDS") or data.get("gdelt_request_delay_seconds", data.get("request_delay_seconds", 12.0))),
        max_retries=int(os.getenv("GDELT_MAX_RETRIES") or data.get("gdelt_max_retries", data.get("max_retries", 2))),
        retry_delay_seconds=float(os.getenv("GDELT_RETRY_DELAY_SECONDS") or data.get("gdelt_retry_delay_seconds", data.get("retry_delay_seconds", 15.0))),
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
