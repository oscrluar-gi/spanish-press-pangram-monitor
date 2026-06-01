from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import datetime, time
from pathlib import Path
from typing import Any

from src.utils import MADRID_TZ, parse_datetime, parse_target_date
from src.wayback_client import WaybackClient, find_best_snapshot, load_wayback_config


@dataclass(frozen=True)
class WaybackAuditRow:
    media_name: str
    url: str
    has_snapshot: bool
    best_snapshot_timestamp: str | None
    distance_hours: float | None
    source_api: str | None
    statuscode: str | None
    mimetype: str | None
    error_message: str | None = None


def audit_wayback(rows: list[Any], target_date: str, config_path: str = "config/media.yaml") -> tuple[list[dict[str, object]], Path, Path]:
    config = load_wayback_config(config_path)
    results: list[WaybackAuditRow] = []
    with WaybackClient(
        request_delay_seconds=config.request_delay_seconds,
        max_retries=config.max_retries,
    ) as client:
        for row in rows:
            target_datetime = _target_datetime_for_row(row)
            try:
                snapshot = find_best_snapshot(client, row["url"], target_datetime, config) if config.enabled else None
                results.append(
                    WaybackAuditRow(
                        media_name=row["media_name"],
                        url=row["url"],
                        has_snapshot=snapshot is not None,
                        best_snapshot_timestamp=snapshot.timestamp if snapshot else None,
                        distance_hours=round((snapshot.distance_seconds or 0) / 3600, 2) if snapshot else None,
                        source_api=snapshot.source_api if snapshot else None,
                        statuscode=str(snapshot.statuscode) if snapshot and snapshot.statuscode is not None else None,
                        mimetype=snapshot.mimetype if snapshot else None,
                    )
                )
            except Exception as exc:
                results.append(
                    WaybackAuditRow(
                        media_name=row["media_name"],
                        url=row["url"],
                        has_snapshot=False,
                        best_snapshot_timestamp=None,
                        distance_hours=None,
                        source_api=None,
                        statuscode=None,
                        mimetype=None,
                        error_message=str(exc),
                    )
                )
    export_rows = [row.__dict__ for row in results]
    out_dir = Path("exports/audits")
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / f"wayback_audit_{target_date}.csv"
    json_path = out_dir / f"wayback_audit_{target_date}.json"
    _write_csv(csv_path, export_rows)
    json_path.write_text(json.dumps(export_rows, ensure_ascii=False, indent=2), encoding="utf-8")
    return export_rows, csv_path, json_path


def _target_datetime_for_row(row: Any):
    for key in ("rss_published_at", "discovered_lastmod", "lastmod"):
        try:
            value = row[key]
        except (KeyError, IndexError):
            value = None
        parsed = parse_datetime(value)
        if parsed:
            return parsed
    target = parse_target_date(row["target_date"])
    return datetime.combine(target, time(12, 0), tzinfo=MADRID_TZ)


def _write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    fieldnames = list(WaybackAuditRow.__dataclass_fields__.keys())
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
