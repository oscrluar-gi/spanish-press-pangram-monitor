from __future__ import annotations

import csv
import json
import logging
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Optional

import typer

if __package__ in {None, ""}:
    sys.path.append(str(Path(__file__).resolve().parents[1]))

from src import database
from src.audit import audit_sources as run_audit_sources
from src.discover import discover_all, load_media_config
from src.extract import iter_extract_many
from src.pangram_client import PangramError, analyze_text
from src.source_probes import DEFAULT_STRATEGIES, probe_sources
from src.utils import load_environment, parse_datetime, parse_target_date, parse_time_window, setup_logging
from src.validation import export_validation
from src.wayback_client import WaybackSnapshot
from src.wayback_audit import audit_wayback as run_audit_wayback

LOGGER = logging.getLogger(__name__)
app = typer.Typer(help="Monitor Spanish press articles and analyze them with Pangram.")


def _prepare_db() -> tuple[object, str]:
    conn = database.connect()
    database.initialize_database(conn)
    return conn, database.get_db_path()


def _validate_wayback_mode(value: str) -> None:
    if value not in {"off", "fallback", "always"}:
        raise typer.BadParameter("Wayback mode must be one of: off, fallback, always")


def _save_article_wayback_snapshot(conn: object, article: object) -> None:
    if not getattr(article, "wayback_timestamp", None):
        return
    snapshot = WaybackSnapshot(
        timestamp=article.wayback_timestamp,
        original_url=article.original_url or article.url,
        mimetype=article.wayback_mimetype,
        statuscode=article.wayback_statuscode,
        digest=article.wayback_digest,
        snapshot_url=None,
        source_api=article.wayback_source_api or "cdx",
        distance_seconds=article.wayback_distance_seconds,
    )
    database.save_wayback_snapshot(conn, article.normalized_url, snapshot, selected=article.content_source == "wayback")


def _build_time_window(date: str, start_time: str | None, end_time: str | None, hours: int | None) -> tuple[datetime, datetime] | None:
    try:
        return parse_time_window(date, start_time=start_time, end_time=end_time, hours=hours)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc


def _row_in_time_window(row: object, time_window: tuple[datetime, datetime] | None) -> bool:
    if time_window is None:
        return True
    start, end = time_window
    for key in ("rss_published_at", "discovered_lastmod", "lastmod"):
        try:
            value = row[key]  # type: ignore[index]
        except (KeyError, IndexError):
            value = None
        parsed = parse_datetime(value)
        if parsed:
            return start <= parsed < end
    return False


def _time_window_label(time_window: tuple[datetime, datetime] | None) -> str:
    if time_window is None:
        return ""
    start, end = time_window
    return f" window={start.isoformat()}..{end.isoformat()}"


@app.command()
def discover(
    date: str = typer.Option(..., "--date", help="Target publication date in YYYY-MM-DD format."),
    config: str = typer.Option("config/media.yaml", "--config", help="Media YAML config path."),
    media: Optional[str] = typer.Option(None, "--media", help="Only discover rows for media whose name or domain contains this value."),
    start_time: Optional[str] = typer.Option(None, "--start-time", help="Optional start time in HH:MM Europe/Madrid."),
    end_time: Optional[str] = typer.Option(None, "--end-time", help="Optional end time in HH:MM Europe/Madrid."),
    hours: Optional[int] = typer.Option(None, "--hours", help="Optional window length in hours from --start-time."),
) -> None:
    """Discover article URLs from sitemaps and configured RSS feeds."""
    setup_logging()
    parse_target_date(date)
    time_window = _build_time_window(date, start_time, end_time, hours)
    conn, _ = _prepare_db()
    database.upsert_media_many(conn, load_media_config(config))
    items = discover_all(config, date, time_window=time_window, media_filter=media)
    inserted = 0
    for item in items:
        inserted += int(database.save_discovered_url(conn, item))
    window_label = _time_window_label(time_window)
    media_label = f" media={media}" if media else ""
    database.log_run(conn, "discover", date, "ok", f"{inserted} new URLs, {len(items)} candidates{window_label}{media_label}")
    typer.echo(f"Discovery complete: {len(items)} candidates, {inserted} new URLs.{window_label}{media_label}")


@app.command()
def extract(
    date: str = typer.Option(..., "--date", help="Target publication date in YYYY-MM-DD format."),
    wayback: str = typer.Option("fallback", "--wayback", help="Wayback mode: off, fallback, always."),
    config: str = typer.Option("config/media.yaml", "--config", help="Media YAML config path."),
    media: Optional[str] = typer.Option(None, "--media", help="Only extract rows for media whose name contains this value."),
    limit: Optional[int] = typer.Option(None, "--limit", help="Maximum number of discovered URLs to extract."),
    only_failed: bool = typer.Option(False, "--only-failed", help="Only retry rows that are missing or not already ok."),
    start_time: Optional[str] = typer.Option(None, "--start-time", help="Optional start time in HH:MM Europe/Madrid."),
    end_time: Optional[str] = typer.Option(None, "--end-time", help="Optional end time in HH:MM Europe/Madrid."),
    hours: Optional[int] = typer.Option(None, "--hours", help="Optional window length in hours from --start-time."),
) -> None:
    """Extract clean article text for discovered URLs."""
    setup_logging()
    load_environment()
    parse_target_date(date)
    time_window = _build_time_window(date, start_time, end_time, hours)
    _validate_wayback_mode(wayback)
    conn, _ = _prepare_db()
    rows = database.discovered_for_date(conn, date)
    rows = [row for row in rows if _row_in_time_window(row, time_window)]
    if media:
        needle = media.lower()
        rows = [row for row in rows if needle in str(row["media_name"]).lower()]
    if only_failed:
        ok_statuses = {"ok", "ok_live", "ok_wayback"}
        rows = [row for row in rows if row["current_extraction_status"] not in ok_statuses]
    if limit is not None:
        rows = rows[: max(0, limit)]
    if not rows:
        database.log_run(conn, "extract", date, "ok", "No discovered URLs")
        typer.echo("No discovered URLs for that date. Run discover first.")
        return

    ok = 0
    for row, article in iter_extract_many(rows, wayback_mode=wayback, config_path=config):
        database.save_article(
            conn,
            discovered_url_id=int(row["id"]),
            media_id=int(row["media_id"]),
            media_name=row["media_name"],
            domain=row["domain"],
            article=article,
        )
        _save_article_wayback_snapshot(conn, article)
        ok += int(article.extraction_status in {"ok", "ok_live", "ok_wayback"})
    window_label = _time_window_label(time_window)
    database.log_run(conn, "extract", date, "ok", f"{ok} articles extracted from {len(rows)} URLs{window_label}")
    typer.echo(f"Extraction complete: {ok} ok articles from {len(rows)} URLs.{window_label}")


@app.command("analyze")
def analyze_command(
    date: str = typer.Option(..., "--date", help="Target publication date in YYYY-MM-DD format."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be sent without calling Pangram."),
    limit: Optional[int] = typer.Option(None, "--limit", help="Maximum number of articles to analyze."),
    force: bool = typer.Option(False, "--force", help="Reanalyze articles even if they already have Pangram results."),
    include_incomplete: bool = typer.Option(False, "--include-incomplete", help="Also send too_short and paywall_or_incomplete articles."),
) -> None:
    """Analyze extracted articles with Pangram, reusing duplicate text hashes."""
    setup_logging()
    load_environment()
    parse_target_date(date)
    conn, _ = _prepare_db()
    rows = database.articles_ready_for_analysis(
        conn,
        date,
        include_incomplete=include_incomplete,
        force=force,
        limit=limit,
    )
    if dry_run:
        typer.echo(f"Dry run: {len(rows)} articles would be sent to Pangram.")
        if not rows and not force:
            forced_rows = database.articles_ready_for_analysis(
                conn,
                date,
                include_incomplete=include_incomplete,
                force=True,
                limit=limit,
            )
            pangram_counts = database.pangram_status_counts_for_date(conn, date)
            if forced_rows and pangram_counts.get("error", 0):
                typer.echo(
                    f"{len(forced_rows)} valid articles already have Pangram attempts, "
                    f"including {pangram_counts['error']} failed attempt(s). "
                    "Fix the API key or transient issue, then retry with --force."
                )
        for row in rows[:10]:
            typer.echo(f"- {row['media_name']}: {row['url']} ({row['word_count']} words)")
        database.log_run(conn, "analyze_dry_run", date, "ok", f"{len(rows)} candidates")
        return
    if not rows:
        database.log_run(conn, "analyze", date, "ok", "No articles ready")
        typer.echo("No extracted articles ready for analysis.")
        return

    analyzed = 0
    reused = 0
    failed = 0
    for row in rows:
        text_hash = row["text_hash"]
        prior = None if force else database.pangram_result_for_hash(conn, text_hash)
        if prior:
            raw = prior["raw_response_json"] or prior["response_json"]
            response = json.loads(raw)
            database.save_pangram_result(conn, int(row["id"]), text_hash, response, "reused")
            reused += 1
            continue
        try:
            response = analyze_text(row["text_clean"])
            database.save_pangram_result(conn, int(row["id"]), text_hash, response, "ok")
            analyzed += 1
        except PangramError as exc:
            database.save_pangram_result(conn, int(row["id"]), text_hash, None, "error", str(exc))
            LOGGER.warning("Pangram failed for article %s: %s", row["id"], exc)
            failed += 1
    database.log_run(conn, "analyze", date, "ok", f"{analyzed} analyzed, {reused} reused, {failed} failed")
    typer.echo(f"Analysis complete: {analyzed} analyzed, {reused} reused, {failed} failed.")


@app.command()
def run(
    date: str = typer.Option(..., "--date", help="Target publication date in YYYY-MM-DD format."),
    config: str = typer.Option("config/media.yaml", "--config", help="Media YAML config path."),
    wayback: str = typer.Option("fallback", "--wayback", help="Wayback mode: off, fallback, always."),
) -> None:
    """Run the full discovery, extraction, and Pangram analysis pipeline."""
    load_environment()
    _validate_wayback_mode(wayback)
    discover(date=date, config=config)
    extract(date=date, wayback=wayback, config=config)
    analyze_command(date=date)


@app.command("audit-sources")
def audit_sources_command(
    date: str = typer.Option(..., "--date", help="Target publication date in YYYY-MM-DD format."),
    config: str = typer.Option("config/media.yaml", "--config", help="Media YAML config path."),
) -> None:
    """Audit configured sitemaps/RSS and export source coverage reports."""
    setup_logging()
    rows, csv_path, json_path = run_audit_sources(config, date)
    typer.echo(f"Audit complete: {len(rows)} sources.")
    typer.echo(f"CSV: {csv_path}")
    typer.echo(f"JSON: {json_path}")
    typer.echo(
        "audit-sources is diagnostic only. It does not write URLs to SQLite. "
        f"To ingest URLs, run: python -m src.main discover --date {date}"
    )


@app.command("audit-wayback")
def audit_wayback_command(
    date: str = typer.Option(..., "--date", help="Target publication date in YYYY-MM-DD format."),
    config: str = typer.Option("config/media.yaml", "--config", help="Media YAML config path."),
) -> None:
    """Audit Wayback snapshot availability for discovered URLs without downloading article content."""
    setup_logging()
    load_environment()
    parse_target_date(date)
    conn, _ = _prepare_db()
    rows = database.discovered_for_date(conn, date)
    export_rows, csv_path, json_path = run_audit_wayback(rows, date, config)
    typer.echo(f"Wayback audit complete: {len(export_rows)} URLs.")
    typer.echo(f"CSV: {csv_path}")
    typer.echo(f"JSON: {json_path}")


@app.command("probe-sources")
def probe_sources_command(
    date: str = typer.Option(..., "--date", help="Target publication date in YYYY-MM-DD format."),
    config: str = typer.Option("config/media.yaml", "--config", help="Media YAML config path."),
    media: Optional[str] = typer.Option(None, "--media", help="Comma-separated media/name/domain filter."),
    strategies: str = typer.Option(
        ",".join(DEFAULT_STRATEGIES),
        "--strategies",
        help="Comma-separated: wayback_robots,wayback_rss,gdelt,web_search.",
    ),
    max_results: int = typer.Option(25, "--max-results", help="Maximum candidate rows per strategy/media."),
) -> None:
    """Experiment with non-ingesting discovery alternatives and export CSV/JSON."""
    setup_logging()
    load_environment()
    parse_target_date(date)
    selected = [item.strip() for item in strategies.split(",") if item.strip()]
    try:
        rows, csv_path, json_path = probe_sources(
            config,
            date,
            media_filter=media,
            strategies=selected,
            max_results=max_results,
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    ok = sum(1 for row in rows if row.get("status", "").startswith("ok"))
    typer.echo(f"Source probe complete: {ok} useful rows from {len(rows)} total rows.")
    typer.echo("This command is diagnostic only. It does not write URLs to SQLite.")
    typer.echo(f"CSV: {csv_path}")
    typer.echo(f"JSON: {json_path}")


@app.command()
def export(
    date: str = typer.Option(..., "--date", help="Target publication date in YYYY-MM-DD format."),
    format: str = typer.Option("csv", "--format", help="Export format. Currently only csv is supported."),
    output: Optional[str] = typer.Option(None, "--output", help="Output path."),
) -> None:
    """Export structured results for a date."""
    setup_logging()
    parse_target_date(date)
    if format.lower() != "csv":
        raise typer.BadParameter("Only csv export is supported in the MVP")

    conn, _ = _prepare_db()
    rows = database.export_rows(conn, date)
    out_path = Path(output or f"exports/press_{date}.csv")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "media_name",
        "url",
        "title",
        "published_at",
        "word_count",
        "extraction_status",
        "pangram_prediction",
        "pangram_score",
        "pangram_response_json",
    ]
    with out_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    database.log_run(conn, "export", date, "ok", str(out_path))
    typer.echo(f"Exported {len(rows)} rows to {out_path}.")


@app.command()
def report(date: str = typer.Option(..., "--date", help="Target publication date in YYYY-MM-DD format.")) -> None:
    """Show and export a coverage/error report for one date."""
    setup_logging()
    parse_target_date(date)
    conn, _ = _prepare_db()
    counts = database.table_counts(conn, date)
    if counts["discovered_urls"] == 0:
        typer.echo("No discovered URLs found. Run discover first.")
    rows = database.report_rows(conn, date)
    errors = database.error_rows(conn, date)
    out_dir = Path("exports/reports")
    out_dir.mkdir(parents=True, exist_ok=True)
    report_path = out_dir / f"report_{date}.csv"
    fieldnames = [
        "media_name",
        "discovered",
        "extracted_ok",
        "live_ok",
        "wayback_ok",
        "wayback_hits",
        "wayback_misses",
        "avg_wayback_distance_hours",
        "pangram_sent",
        "extraction_failures",
        "extraction_coverage_pct",
    ]
    export_rows = []
    for row in rows:
        discovered = int(row["discovered"] or 0)
        extracted = int(row["extracted_ok"] or 0)
        coverage = round((extracted / discovered * 100), 2) if discovered else 0.0
        avg_seconds = row.get("avg_wayback_distance_seconds")
        export_rows.append(
            {
                **row,
                "avg_wayback_distance_hours": round(float(avg_seconds) / 3600, 2) if avg_seconds is not None else None,
                "extraction_coverage_pct": coverage,
            }
        )
    with report_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(export_rows)

    error_counter = Counter(row.get("error_type") or row["extraction_status"] for row in errors)
    failure_by_media = Counter(row["media_name"] for row in errors)
    typer.echo(f"Report for {date}")
    for row in export_rows:
        typer.echo(
            f"- {row['media_name']}: discovered={row['discovered']} "
            f"extracted_ok={row['extracted_ok']} live={row['live_ok']} wayback={row['wayback_ok']} "
            f"pangram={row['pangram_sent']} "
            f"coverage={row['extraction_coverage_pct']}%"
        )
    typer.echo(f"Errors by type: {dict(error_counter)}")
    typer.echo(f"Top failure media: {dict(failure_by_media.most_common(5))}")
    typer.echo(f"Wayback helped most: {[(row['media_name'], row['wayback_ok']) for row in sorted(export_rows, key=lambda item: item.get('wayback_ok') or 0, reverse=True)[:5]]}")
    typer.echo(f"Omitted articles: {len(errors)}")
    typer.echo(f"CSV: {report_path}")
    database.log_run(conn, "report", date, "ok", str(report_path))


@app.command()
def validate(
    date: str = typer.Option(..., "--date", help="Target publication date in YYYY-MM-DD format."),
    sample_size: int = typer.Option(10, "--sample-size", help="Maximum manual sample rows per media."),
    config: str = typer.Option("config/media.yaml", "--config", help="Media YAML config path."),
) -> None:
    """Validate stored pipeline output without calling external services."""
    setup_logging()
    parse_target_date(date)
    conn, _ = _prepare_db()
    database.upsert_media_many(conn, load_media_config(config))
    if database.has_no_ingested_data(conn, date):
        typer.echo(
            "WARNING: No ingested data found for this date. It looks like discover/extract "
            "have not been run against the current SQLite database."
        )
    rows = database.validation_article_rows(conn, date)
    summary, full, samples, summary_path, json_path, sample_path = export_validation(rows, date, sample_size)
    typer.echo(f"Validation complete for {date}.")
    typer.echo(f"Media: {len(summary)}")
    typer.echo(f"Warnings: {len(full['warnings'])}")
    typer.echo(f"Sample rows: {len(samples)}")
    typer.echo(f"Summary CSV: {summary_path}")
    typer.echo(f"JSON: {json_path}")
    typer.echo(f"Sample CSV: {sample_path}")
    database.log_run(conn, "validate", date, "ok", str(summary_path))


@app.command()
def status(date: str = typer.Option(..., "--date", help="Target date in YYYY-MM-DD format.")) -> None:
    """Show which SQLite database is being used and row counts for a date."""
    setup_logging()
    parse_target_date(date)
    conn, db_path = _prepare_db()
    counts = database.table_counts(conn, date)
    typer.echo(f"database_path: {db_path}")
    typer.echo(f"media_count: {counts['media']}")
    typer.echo(f"discovered_urls_count: {counts['discovered_urls']}")
    typer.echo(f"articles_count: {counts['articles']}")
    typer.echo(f"pangram_results_count: {counts['pangram_results']}")
    typer.echo(f"wayback_snapshots_count: {counts['wayback_snapshots']}")
    typer.echo("run_log_last_10:")
    rows = database.latest_run_log(conn, limit=10)
    if not rows:
        typer.echo("- no run_log entries")
    for row in rows:
        target = row["target_date"] or "-"
        message = row["message"] or ""
        typer.echo(f"- {row['created_at']} {row['run_type']} date={target} status={row['status']} {message}")


if __name__ == "__main__":
    app()
