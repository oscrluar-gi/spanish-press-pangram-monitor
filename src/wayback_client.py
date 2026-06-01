from __future__ import annotations

import logging
import os
import random
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import yaml

from src.models import WaybackConfig
from src.utils import MADRID_TZ, build_request_headers

LOGGER = logging.getLogger(__name__)
WAYBACK_AVAILABILITY_URL = "https://archive.org/wayback/available"
WAYBACK_CDX_URL = "https://web.archive.org/cdx/search/cdx"


@dataclass(frozen=True)
class WaybackSnapshot:
    timestamp: str
    original_url: str
    mimetype: str | None = None
    statuscode: str | int | None = None
    digest: str | None = None
    snapshot_url: str | None = None
    source_api: str = "cdx"
    distance_seconds: int | None = None
    error_message: str | None = None


class WaybackClient:
    def __init__(
        self,
        *,
        timeout_seconds: float = 20.0,
        request_delay_seconds: float = 1.5,
        max_retries: int = 2,
        client: httpx.Client | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.request_delay_seconds = request_delay_seconds
        self.max_retries = max_retries
        self._client = client
        self.headers = headers or build_request_headers()
        self._last_request = 0.0
        self._lock = threading.Lock()
        self._request_lock = threading.Lock()

    def __enter__(self) -> "WaybackClient":
        if self._client is None:
            self._client = httpx.Client(timeout=self.timeout_seconds, follow_redirects=True, headers=self.headers)
        return self

    def __exit__(self, *args: object) -> None:
        if self._client is not None:
            self._client.close()

    def find_snapshots_cdx(
        self,
        url: str,
        target_datetime: datetime,
        max_days_before: int,
        max_days_after: int,
    ) -> list[WaybackSnapshot]:
        start = target_datetime - timedelta(days=max_days_before)
        end = target_datetime + timedelta(days=max_days_after)
        params = {
            "url": url,
            "from": format_wayback_timestamp(start),
            "to": format_wayback_timestamp(end),
            "output": "json",
            "filter": ["statuscode:200", "mimetype:text/html"],
            "collapse": "digest",
            "fl": "timestamp,original,mimetype,statuscode,digest",
        }
        response = self._get(WAYBACK_CDX_URL, params=params)
        snapshots = parse_cdx_response(response.json(), target_datetime)
        return [
            snapshot
            for snapshot in snapshots
            if _within_window(snapshot, target_datetime, max_days_before, max_days_after)
        ]

    def find_domain_snapshots_cdx(
        self,
        domain: str,
        start_datetime: datetime,
        end_datetime: datetime,
        *,
        limit: int,
    ) -> list[WaybackSnapshot]:
        params = {
            "url": domain,
            "matchType": "domain",
            "from": format_wayback_timestamp(start_datetime),
            "to": format_wayback_timestamp(end_datetime),
            "output": "json",
            "filter": ["statuscode:200", "mimetype:text/html"],
            "collapse": "urlkey",
            "fl": "timestamp,original,mimetype,statuscode,digest",
            "limit": str(limit),
        }
        response = self._get(WAYBACK_CDX_URL, params=params)
        snapshots = parse_cdx_response(response.json(), start_datetime)
        return sorted(snapshots, key=lambda item: (item.original_url, item.timestamp))

    def find_url_pattern_snapshots_cdx(
        self,
        url_pattern: str,
        start_datetime: datetime,
        end_datetime: datetime,
        *,
        limit: int,
    ) -> list[WaybackSnapshot]:
        params = {
            "url": url_pattern,
            "from": format_wayback_timestamp(start_datetime),
            "to": format_wayback_timestamp(end_datetime),
            "output": "json",
            "filter": ["statuscode:200", "mimetype:text/html"],
            "collapse": "urlkey",
            "fl": "timestamp,original,mimetype,statuscode,digest",
            "limit": str(limit),
        }
        response = self._get(WAYBACK_CDX_URL, params=params)
        snapshots = parse_cdx_response(response.json(), start_datetime)
        return sorted(snapshots, key=lambda item: (item.original_url, item.timestamp))

    def find_snapshot_availability(self, url: str, target_datetime: datetime) -> WaybackSnapshot | None:
        params = {"url": url, "timestamp": format_wayback_timestamp(target_datetime)}
        response = self._get(WAYBACK_AVAILABILITY_URL, params=params)
        return parse_availability_response(response.json(), url, target_datetime)

    def _get(self, url: str, params: dict[str, Any]) -> httpx.Response:
        assert self._client is not None
        last_exc: Exception | None = None
        with self._request_lock:
            for attempt in range(self.max_retries + 1):
                try:
                    self._rate_limit()
                    response = self._client.get(url, params=params)
                    if response.status_code == 429 or response.status_code >= 500:
                        if attempt < self.max_retries:
                            delay = _backoff_delay(attempt)
                            LOGGER.info("Retrying Wayback %s in %.2fs after HTTP %s", url, delay, response.status_code)
                            time.sleep(delay)
                            continue
                    response.raise_for_status()
                    return response
                except httpx.HTTPError as exc:
                    last_exc = exc
                    if attempt < self.max_retries:
                        delay = _backoff_delay(attempt)
                        LOGGER.info("Retrying Wayback %s in %.2fs after %s", url, delay, exc)
                        time.sleep(delay)
                        continue
                    break
        raise RuntimeError(f"Wayback request failed: {last_exc}")

    def _rate_limit(self) -> None:
        with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_request
            remaining = self.request_delay_seconds - elapsed
            if remaining > 0:
                time.sleep(remaining)
            self._last_request = time.monotonic()


def find_snapshots_cdx(
    url: str,
    target_datetime: datetime,
    max_days_before: int,
    max_days_after: int,
) -> list[WaybackSnapshot]:
    with WaybackClient() as client:
        return client.find_snapshots_cdx(url, target_datetime, max_days_before, max_days_after)


def find_snapshot_availability(url: str, target_datetime: datetime) -> WaybackSnapshot | None:
    with WaybackClient() as client:
        return client.find_snapshot_availability(url, target_datetime)


def load_wayback_config(path: str = "config/media.yaml") -> WaybackConfig:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle) or {}
    except FileNotFoundError:
        raw = {}
    data = raw.get("wayback", {})
    return WaybackConfig(
        enabled=_bool_env("WAYBACK_ENABLED", data.get("wayback_enabled", data.get("enabled", True))),
        strategy=str(os.getenv("WAYBACK_STRATEGY") or data.get("wayback_strategy", data.get("strategy", "fallback_only"))),
        max_days_after=int(os.getenv("WAYBACK_MAX_DAYS_AFTER") or data.get("wayback_max_days_after", data.get("max_days_after", 7))),
        max_days_before=int(os.getenv("WAYBACK_MAX_DAYS_BEFORE") or data.get("wayback_max_days_before", data.get("max_days_before", 1))),
        request_delay_seconds=float(os.getenv("WAYBACK_REQUEST_DELAY_SECONDS") or data.get("wayback_request_delay_seconds", data.get("request_delay_seconds", 1.5))),
        max_retries=int(os.getenv("WAYBACK_MAX_RETRIES") or data.get("wayback_max_retries", data.get("max_retries", 2))),
        timeout_seconds=float(os.getenv("WAYBACK_TIMEOUT_SECONDS") or data.get("wayback_timeout_seconds", data.get("timeout_seconds", 45.0))),
        use_cdx=_bool_env("WAYBACK_USE_CDX", data.get("wayback_use_cdx", data.get("use_cdx", True))),
        use_availability=_bool_env("WAYBACK_USE_AVAILABILITY", data.get("wayback_use_availability", data.get("use_availability", True))),
        extended_search=_bool_env("WAYBACK_EXTENDED_SEARCH", data.get("wayback_extended_search", data.get("extended_search", True))),
        extended_max_days_after=int(os.getenv("WAYBACK_EXTENDED_MAX_DAYS_AFTER") or data.get("wayback_extended_max_days_after", data.get("extended_max_days_after", 30))),
        extended_max_days_before=int(os.getenv("WAYBACK_EXTENDED_MAX_DAYS_BEFORE") or data.get("wayback_extended_max_days_before", data.get("extended_max_days_before", 7))),
        discovery_enabled=_bool_env("WAYBACK_DISCOVERY_ENABLED", data.get("wayback_discovery_enabled", data.get("discovery_enabled", True))),
        discovery_max_urls_per_media=int(os.getenv("WAYBACK_DISCOVERY_MAX_URLS_PER_MEDIA") or data.get("wayback_discovery_max_urls_per_media", data.get("discovery_max_urls_per_media", 250))),
        discovery_max_days_after=int(os.getenv("WAYBACK_DISCOVERY_MAX_DAYS_AFTER") or data.get("wayback_discovery_max_days_after", data.get("discovery_max_days_after", 3))),
        discovery_max_days_before=int(os.getenv("WAYBACK_DISCOVERY_MAX_DAYS_BEFORE") or data.get("wayback_discovery_max_days_before", data.get("discovery_max_days_before", 0))),
        discovery_timeout_seconds=float(os.getenv("WAYBACK_DISCOVERY_TIMEOUT_SECONDS") or data.get("wayback_discovery_timeout_seconds", data.get("discovery_timeout_seconds", 15.0))),
        discovery_max_retries=int(os.getenv("WAYBACK_DISCOVERY_MAX_RETRIES") or data.get("wayback_discovery_max_retries", data.get("discovery_max_retries", 0))),
        discovery_min_candidates=int(os.getenv("WAYBACK_DISCOVERY_MIN_CANDIDATES") or data.get("wayback_discovery_min_candidates", data.get("discovery_min_candidates", 2))),
    )


def find_best_snapshot(
    client: WaybackClient,
    url: str,
    target_datetime: datetime,
    config: WaybackConfig,
) -> WaybackSnapshot | None:
    snapshots, errors = _find_snapshots_for_window(
        client,
        url,
        target_datetime,
        config,
        config.max_days_before,
        config.max_days_after,
    )
    selected = select_best_snapshot(snapshots, target_datetime, config.max_days_before, config.max_days_after)
    if selected:
        return selected

    if not snapshots and errors:
        raise RuntimeError("; ".join(errors))

    if config.extended_search:
        extended_before = max(config.max_days_before, config.extended_max_days_before)
        extended_after = max(config.max_days_after, config.extended_max_days_after)
        if (extended_before, extended_after) != (config.max_days_before, config.max_days_after):
            extended_snapshots, extended_errors = _find_snapshots_for_window(
                client,
                url,
                target_datetime,
                config,
                extended_before,
                extended_after,
            )
            selected = select_best_snapshot(extended_snapshots, target_datetime, extended_before, extended_after)
            if selected:
                return selected
            if not extended_snapshots and extended_errors:
                raise RuntimeError("; ".join(extended_errors))
    return None


def _find_snapshots_for_window(
    client: WaybackClient,
    url: str,
    target_datetime: datetime,
    config: WaybackConfig,
    max_days_before: int,
    max_days_after: int,
) -> tuple[list[WaybackSnapshot], list[str]]:
    snapshots: list[WaybackSnapshot] = []
    errors: list[str] = []
    if config.use_cdx:
        try:
            snapshots.extend(client.find_snapshots_cdx(url, target_datetime, max_days_before, max_days_after))
        except Exception as exc:
            errors.append(f"cdx: {exc}")
            LOGGER.info("Wayback CDX failed for %s: %s", url, exc)
    if config.use_availability:
        try:
            snapshot = client.find_snapshot_availability(url, target_datetime)
            if snapshot:
                snapshots.append(snapshot)
        except Exception as exc:
            errors.append(f"availability: {exc}")
            LOGGER.info("Wayback Availability failed for %s: %s", url, exc)
    return snapshots, errors


def parse_cdx_response(payload: Any, target_datetime: datetime) -> list[WaybackSnapshot]:
    if not isinstance(payload, list) or not payload:
        return []
    header = payload[0]
    rows = payload[1:] if all(isinstance(item, str) for item in header) else payload
    snapshots: list[WaybackSnapshot] = []
    for row in rows:
        if not isinstance(row, list):
            continue
        record = dict(zip(header, row)) if all(isinstance(item, str) for item in header) else {}
        timestamp = record.get("timestamp")
        original = record.get("original")
        if not timestamp or not original:
            continue
        snapshot = WaybackSnapshot(
            timestamp=str(timestamp),
            original_url=str(original),
            mimetype=record.get("mimetype"),
            statuscode=record.get("statuscode"),
            digest=record.get("digest"),
            snapshot_url=f"https://web.archive.org/web/{timestamp}/{original}",
            source_api="cdx",
            distance_seconds=timestamp_distance_seconds(str(timestamp), target_datetime),
        )
        snapshots.append(snapshot)
    return sorted(snapshots, key=lambda item: _selection_key(item, target_datetime))


def parse_availability_response(payload: dict[str, Any], original_url: str, target_datetime: datetime) -> WaybackSnapshot | None:
    closest = payload.get("archived_snapshots", {}).get("closest", {}) if isinstance(payload, dict) else {}
    if not closest or not closest.get("available"):
        return None
    status = str(closest.get("status", ""))
    if status != "200":
        return None
    timestamp = closest.get("timestamp")
    snapshot_url = closest.get("url")
    if not timestamp or not snapshot_url:
        return None
    return WaybackSnapshot(
        timestamp=str(timestamp),
        original_url=original_url,
        mimetype="text/html",
        statuscode="200",
        digest=None,
        snapshot_url=str(snapshot_url),
        source_api="availability",
        distance_seconds=timestamp_distance_seconds(str(timestamp), target_datetime),
    )


def select_best_snapshot(
    snapshots: list[WaybackSnapshot],
    target_datetime: datetime,
    max_days_before: int | None = None,
    max_days_after: int | None = None,
) -> WaybackSnapshot | None:
    candidates = []
    for snapshot in snapshots:
        if max_days_before is not None and max_days_after is not None:
            if not _within_window(snapshot, target_datetime, max_days_before, max_days_after):
                continue
        candidates.append(snapshot)
    if not candidates:
        return None
    selected = sorted(candidates, key=lambda item: _selection_key(item, target_datetime))[0]
    return WaybackSnapshot(
        timestamp=selected.timestamp,
        original_url=selected.original_url,
        mimetype=selected.mimetype,
        statuscode=selected.statuscode,
        digest=selected.digest,
        snapshot_url=selected.snapshot_url,
        source_api=selected.source_api,
        distance_seconds=timestamp_distance_seconds(selected.timestamp, target_datetime),
        error_message=selected.error_message,
    )


def build_wayback_raw_url(snapshot: WaybackSnapshot) -> str:
    return f"https://web.archive.org/web/{snapshot.timestamp}id_/{snapshot.original_url}"


def build_wayback_replay_urls(snapshot: WaybackSnapshot) -> list[str]:
    base = f"https://web.archive.org/web/{snapshot.timestamp}"
    return [
        f"{base}id_/{snapshot.original_url}",
        f"{base}if_/{snapshot.original_url}",
        f"{base}/{snapshot.original_url}",
    ]


def format_wayback_timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=MADRID_TZ)
    return value.astimezone(timezone.utc).strftime("%Y%m%d%H%M%S")


def timestamp_to_datetime(timestamp: str) -> datetime:
    return datetime.strptime(timestamp, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)


def timestamp_distance_seconds(timestamp: str, target_datetime: datetime) -> int:
    if target_datetime.tzinfo is None:
        target_datetime = target_datetime.replace(tzinfo=MADRID_TZ)
    return int(abs((timestamp_to_datetime(timestamp) - target_datetime.astimezone(timezone.utc)).total_seconds()))


def _selection_key(snapshot: WaybackSnapshot, target_datetime: datetime) -> tuple[int, int, int]:
    status_ok = 0 if str(snapshot.statuscode) == "200" else 1
    html_ok = 0 if (snapshot.mimetype or "").lower().startswith("text/html") else 1
    distance = timestamp_distance_seconds(snapshot.timestamp, target_datetime)
    is_after = timestamp_to_datetime(snapshot.timestamp) >= target_datetime.astimezone(timezone.utc)
    after_preferred = 0 if is_after else 1
    return (status_ok + html_ok, distance, after_preferred)


def _within_window(snapshot: WaybackSnapshot, target_datetime: datetime, max_days_before: int, max_days_after: int) -> bool:
    if target_datetime.tzinfo is None:
        target_datetime = target_datetime.replace(tzinfo=MADRID_TZ)
    capture = timestamp_to_datetime(snapshot.timestamp)
    start = target_datetime.astimezone(timezone.utc) - timedelta(days=max_days_before)
    end = target_datetime.astimezone(timezone.utc) + timedelta(days=max_days_after)
    return start <= capture <= end


def _backoff_delay(attempt: int) -> float:
    return min(2**attempt, 30) + random.uniform(0, 0.5)


def _bool_env(name: str, default: object) -> bool:
    value = os.getenv(name)
    if value is None:
        return bool(default)
    return value.lower() in {"1", "true", "yes", "on"}
