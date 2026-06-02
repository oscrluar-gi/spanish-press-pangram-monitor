from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from dateutil import parser as date_parser
from dotenv import load_dotenv

MADRID_TZ = ZoneInfo("Europe/Madrid")
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 "
    "SpanishPressPangramMonitor/0.1 (+local research; contact: ops@example.com)"
)
TRACKING_PARAMS_PREFIXES = ("utm_", "xtor")
TRACKING_PARAMS = {
    "fbclid",
    "gclid",
    "dclid",
    "msclkid",
    "cmp",
    "outputtype",
    "ref",
    "referrer",
    "ref_src",
    "smid",
    "ito",
    "ns_campaign",
    "ns_mchannel",
    "ns_source",
    "ns_linkname",
    "ns_fee",
    "cid",
    "mc_cid",
    "mc_eid",
    "pk_campaign",
    "pk_kwd",
}
ALLOWED_QUERY_PARAMS = {
    "id",
    "page",
    "pagina",
    "pos",
    "date",
}
PANGRAM_TEXT_KEYS = {
    "content",
    "input_text",
    "segment_text",
    "text",
    "text_content",
}


def load_environment() -> None:
    load_dotenv()


def setup_logging(level: str | None = None) -> None:
    log_level = (level or os.getenv("LOG_LEVEL") or "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )


def build_request_headers(user_agent: str | None = None) -> dict[str, str]:
    ua = user_agent or os.getenv("PRESS_MONITOR_USER_AGENT") or DEFAULT_USER_AGENT
    if ua.startswith("SpanishPressPangramMonitor/"):
        ua = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 "
            f"{ua}"
        )
    return {
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "es-ES,es;q=0.9,en;q=0.6",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Upgrade-Insecure-Requests": "1",
    }


def parse_target_date(value: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError("Date must use YYYY-MM-DD format") from exc


def start_end_for_date(target_date: date) -> tuple[datetime, datetime]:
    start = datetime.combine(target_date, time.min, tzinfo=MADRID_TZ)
    end = datetime.combine(target_date, time.max, tzinfo=MADRID_TZ)
    return start, end


def parse_time_window(target_date: str, start_time: str | None = None, end_time: str | None = None, hours: int | None = None) -> tuple[datetime, datetime] | None:
    if not start_time and not end_time and hours is None:
        return None
    date_value = parse_target_date(target_date)
    start = _parse_clock_time(start_time or "00:00")
    start_dt = datetime.combine(date_value, start, tzinfo=MADRID_TZ)
    if end_time:
        end = _parse_clock_time(end_time)
        end_dt = datetime.combine(date_value, end, tzinfo=MADRID_TZ)
        if end_dt <= start_dt:
            end_dt += timedelta(days=1)
    else:
        window_hours = hours if hours is not None else 4
        if window_hours <= 0:
            raise ValueError("hours must be greater than zero")
        end_dt = start_dt + timedelta(hours=window_hours)
    return start_dt, end_dt


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = date_parser.parse(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=MADRID_TZ)
    return parsed.astimezone(MADRID_TZ)


def datetime_in_window(value: str | None, start: datetime, end: datetime) -> bool:
    parsed = parse_datetime(value)
    return bool(parsed and start <= parsed < end)


def datetime_matches_target(value: str | None, target_date: date) -> bool:
    parsed = parse_datetime(value)
    return bool(parsed and parsed.date() == target_date)


def normalize_article_url(url: str) -> str:
    split = urlsplit(url.strip())
    scheme = (split.scheme or "https").lower()
    netloc = split.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    path = re.sub(r"/{2,}", "/", split.path or "/")
    if path != "/" and path.endswith("/"):
        path = path[:-1]
    kept_query = []
    for key, value in parse_qsl(split.query, keep_blank_values=False):
        key_lower = key.lower()
        if key_lower in TRACKING_PARAMS or key_lower.startswith(TRACKING_PARAMS_PREFIXES):
            continue
        if key_lower not in ALLOWED_QUERY_PARAMS:
            continue
        kept_query.append((key, value))
    query = urlencode(sorted(kept_query))
    return urlunsplit((scheme, netloc, path, query, ""))


def normalize_url(url: str) -> str:
    return normalize_article_url(url)


def text_sha256(text: str) -> str:
    normalized = normalize_text_whitespace(text)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def normalize_text_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def count_words(text: str | None) -> int:
    if not text:
        return 0
    return len(re.findall(r"\b\w+\b", text, flags=re.UNICODE))


def ensure_parent_dir(path: str | Path) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)


def url_date_hint(url: str) -> date | None:
    patterns = (
        r"/(20\d{2})/([01]\d)/([0-3]\d)(?:/|[-_.])",
        r"(20\d{2})-([01]\d)-([0-3]\d)",
        r"(20\d{2})([01]\d)([0-3]\d)",
    )
    for pattern in patterns:
        match = re.search(pattern, url)
        if not match:
            continue
        year, month, day = map(int, match.groups())
        try:
            return date(year, month, day)
        except ValueError:
            continue
    return None


def url_month_hint(url: str) -> tuple[int, int] | None:
    patterns = (
        r"/(20\d{2})/([01]\d)(?:/|[-_.])",
        r"(20\d{2})-([01]\d)(?:-|/|_)",
        r"(20\d{2})([01]\d)(?:[^\d]|$)",
    )
    for pattern in patterns:
        match = re.search(pattern, url)
        if not match:
            continue
        year, month = map(int, match.groups())
        if 1 <= month <= 12:
            return year, month
    return None


def response_summary(response: dict[str, Any] | None) -> tuple[Any, Any]:
    if not response:
        return None, None

    prediction_keys = ("prediction", "label", "classification", "verdict")
    score_keys = ("score", "ai_score", "ai_probability", "probability", "percent_ai")
    prediction = _first_value_for_keys(response, prediction_keys)
    score = _first_value_for_keys(response, score_keys)
    return prediction, score


def response_model_version(response: dict[str, Any] | None) -> Any:
    if not response:
        return None
    return _first_value_for_keys(response, ("model_version", "modelVersion", "version"))


def sanitize_pangram_response(value: Any) -> Any:
    """Remove text-bearing fields from a Pangram response before persistence."""
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, child in value.items():
            if key.lower() in PANGRAM_TEXT_KEYS:
                cleaned[key] = "[redacted]"
            else:
                cleaned[key] = sanitize_pangram_response(child)
        return cleaned
    if isinstance(value, list):
        return [sanitize_pangram_response(child) for child in value]
    return value


def _first_value_for_keys(value: Any, keys: tuple[str, ...]) -> Any:
    if isinstance(value, dict):
        for key in keys:
            if key in value:
                return value[key]
        for child in value.values():
            found = _first_value_for_keys(child, keys)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _first_value_for_keys(child, keys)
            if found is not None:
                return found
    return None


def _parse_clock_time(value: str) -> time:
    try:
        return datetime.strptime(value, "%H:%M").time()
    except ValueError as exc:
        raise ValueError("Time must use HH:MM format") from exc


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)
