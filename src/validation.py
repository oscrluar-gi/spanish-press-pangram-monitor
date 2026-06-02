from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from src.url_filters import DEFAULT_EXCLUDE_PATTERNS, LIVEBLOG_PATTERNS

OK_STATUSES = {"ok", "ok_live", "ok_wayback"}
NO_TEXT_STATUSES = {"no_text", "wayback_not_found", "wayback_fetch_error", "wayback_parse_error"}
TOO_SHORT_STATUSES = {"too_short", "wayback_too_short"}
PAYWALL_STATUSES = {"paywall_or_incomplete"}
BAD_URL_PATTERNS = tuple(DEFAULT_EXCLUDE_PATTERNS) + tuple(LIVEBLOG_PATTERNS)


def validate_rows(rows: list[dict[str, Any]], target_date: str, sample_size: int = 10) -> tuple[list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[row["media_name"]].append(row)

    summary: list[dict[str, Any]] = []
    samples: list[dict[str, Any]] = []
    full: dict[str, Any] = {"target_date": target_date, "media": {}, "warnings": []}

    for media_name, media_rows in sorted(grouped.items()):
        metrics, warnings = _metrics_for_media(media_name, media_rows)
        summary.append(metrics)
        full["media"][media_name] = {"metrics": metrics, "warnings": warnings}
        full["warnings"].extend({"media_name": media_name, **warning} for warning in warnings)
        samples.extend(_sample_rows(media_rows, sample_size))

    return summary, full, samples


def export_validation(
    rows: list[dict[str, Any]],
    target_date: str,
    sample_size: int = 10,
    output_dir: str | Path = "exports/validation",
) -> tuple[list[dict[str, Any]], dict[str, Any], list[dict[str, Any]], Path, Path, Path]:
    summary, full, samples = validate_rows(rows, target_date, sample_size)
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    summary_path = out_dir / f"validation_summary_{target_date}.csv"
    json_path = out_dir / f"validation_{target_date}.json"
    sample_path = out_dir / f"validation_sample_{target_date}.csv"

    _write_csv(summary_path, summary, SUMMARY_FIELDS)
    json_path.write_text(json.dumps(full, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_csv(sample_path, samples, SAMPLE_FIELDS)
    return summary, full, samples, summary_path, json_path, sample_path


def _metrics_for_media(media_name: str, rows: list[dict[str, Any]]) -> tuple[dict[str, Any], list[dict[str, str]]]:
    discovered = sum(1 for row in rows if row.get("discovered_id"))
    discarded_by_filters = sum(1 for row in rows if row.get("filter_status") and row.get("filter_status") != "included")
    live_ok = sum(1 for row in rows if row.get("content_source") == "live" and row.get("extraction_status") in {"ok", "ok_live"})
    wayback_ok = sum(1 for row in rows if row.get("content_source") == "wayback" and row.get("extraction_status") == "ok_wayback")
    extracted = live_ok + wayback_ok
    no_text = sum(1 for row in rows if row.get("extraction_status") in NO_TEXT_STATUSES)
    too_short = sum(1 for row in rows if row.get("extraction_status") in TOO_SHORT_STATUSES)
    paywall = sum(1 for row in rows if row.get("extraction_status") in PAYWALL_STATUSES)
    pangram = sum(1 for row in rows if row.get("pangram_result_id"))
    extraction_ratio = extracted / discovered if discovered else 0.0
    pangram_ratio = pangram / extracted if extracted else 0.0
    wayback_distances = [
        float(row["wayback_distance_seconds"]) / 3600
        for row in rows
        if row.get("content_source") == "wayback" and row.get("wayback_distance_seconds") is not None
    ]
    avg_wayback_hours = round(sum(wayback_distances) / len(wayback_distances), 2) if wayback_distances else None
    short_under_300 = sum(1 for row in rows if row.get("article_id") and int(row.get("word_count") or 0) < 300)
    article_count = sum(1 for row in rows if row.get("article_id"))
    empty_titles = sum(1 for row in rows if row.get("article_id") and not (row.get("title") or "").strip())
    duplicate_hashes = _duplicate_hash_count(rows)
    leaked_bad_urls = [row.get("url") or row.get("discovered_url") for row in rows if _looks_like_bad_url(row.get("url") or row.get("discovered_url") or "")]

    metrics = {
        "media_name": media_name,
        "discovered_urls": discovered,
        "discarded_by_filters": discarded_by_filters,
        "extracted_live": live_ok,
        "recovered_wayback": wayback_ok,
        "no_text": no_text,
        "too_short": too_short,
        "paywall_or_incomplete": paywall,
        "sent_to_pangram": pangram,
        "extraction_discovery_ratio": round(extraction_ratio, 4),
        "pangram_extraction_ratio": round(pangram_ratio, 4),
        "avg_wayback_distance_hours": avg_wayback_hours,
        "texts_under_300_words": short_under_300,
        "empty_titles": empty_titles,
        "duplicate_hash_count": duplicate_hashes,
        "suspect_url_count": len(leaked_bad_urls),
    }
    warnings = _warnings_for_media(metrics, article_count, short_under_300, empty_titles, duplicate_hashes, leaked_bad_urls, wayback_distances)
    return metrics, warnings


def _warnings_for_media(
    metrics: dict[str, Any],
    article_count: int,
    short_under_300: int,
    empty_titles: int,
    duplicate_hashes: int,
    leaked_bad_urls: list[str],
    wayback_distances: list[float],
) -> list[dict[str, str]]:
    warnings: list[dict[str, str]] = []
    if metrics["discovered_urls"] == 0:
        warnings.append({"code": "no_discovery", "message": "0 URLs discovered"})
    if metrics["discovered_urls"] and metrics["extraction_discovery_ratio"] < 0.4:
        warnings.append({"code": "low_extraction_ratio", "message": "Extraction/discovery ratio below 40%"})
    if article_count and short_under_300 / article_count > 0.5:
        warnings.append({"code": "many_short_texts", "message": "More than 50% of extracted texts have fewer than 300 words"})
    if any(distance > 72 for distance in wayback_distances):
        warnings.append({"code": "far_wayback_snapshot", "message": "At least one Wayback snapshot is more than 72h from target"})
    if article_count and empty_titles / article_count > 0.3:
        warnings.append({"code": "many_empty_titles", "message": "More than 30% of extracted articles have empty titles"})
    if duplicate_hashes > 0:
        warnings.append({"code": "duplicate_hashes", "message": f"{duplicate_hashes} repeated text hashes detected"})
    if leaked_bad_urls:
        warnings.append({"code": "suspect_urls", "message": f"{len(leaked_bad_urls)} section/tag/video/newsletter-like URLs detected"})
    return warnings


def _sample_rows(rows: list[dict[str, Any]], sample_size: int) -> list[dict[str, Any]]:
    article_rows = [row for row in rows if row.get("article_id")]
    selected = sorted(article_rows, key=lambda row: row.get("url") or row.get("discovered_url") or "")[:sample_size]
    return [
        {
            "media_name": row["media_name"],
            "url": row.get("url") or row.get("discovered_url"),
            "title": row.get("title"),
            "published_at": row.get("published_at"),
            "word_count": row.get("word_count"),
            "extraction_status": row.get("extraction_status"),
            "content_source": row.get("content_source"),
            "wayback_timestamp": row.get("wayback_timestamp"),
            "wayback_distance_hours": round(float(row["wayback_distance_seconds"]) / 3600, 2) if row.get("wayback_distance_seconds") is not None else None,
            "text_preview": "",
            "skip_reason": row.get("skip_reason") or row.get("error"),
        }
        for row in selected
    ]


def _duplicate_hash_count(rows: list[dict[str, Any]]) -> int:
    hashes = [row.get("text_hash") for row in rows if row.get("text_hash")]
    counts = Counter(hashes)
    return sum(count - 1 for count in counts.values() if count > 1)


def _looks_like_bad_url(url: str) -> bool:
    lower = url.lower()
    return any(__import__("re").search(pattern, lower) for pattern in BAD_URL_PATTERNS)


def _write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


SUMMARY_FIELDS = [
    "media_name",
    "discovered_urls",
    "discarded_by_filters",
    "extracted_live",
    "recovered_wayback",
    "no_text",
    "too_short",
    "paywall_or_incomplete",
    "sent_to_pangram",
    "extraction_discovery_ratio",
    "pangram_extraction_ratio",
    "avg_wayback_distance_hours",
    "texts_under_300_words",
    "empty_titles",
    "duplicate_hash_count",
    "suspect_url_count",
]

SAMPLE_FIELDS = [
    "media_name",
    "url",
    "title",
    "published_at",
    "word_count",
    "extraction_status",
    "content_source",
    "wayback_timestamp",
    "wayback_distance_hours",
    "text_preview",
    "skip_reason",
]
