# Contributing

Thanks for helping improve the project.

## Local Setup

```powershell
python -m venv .venv
.\.venv\Scripts\activate
pip install -e ".[dev]"
copy .env.example .env
```

Put your own Pangram key in `.env`. Do not commit `.env`.

## Before Opening a PR

Run:

```powershell
python -m pytest
```

Please keep changes focused. If you add a new media source, include:

- the sitemap/RSS/GDELT/Wayback strategy used;
- source-specific include/exclude URL patterns;
- a short note about known limitations;
- tests for parsers or filters when feasible.

## Data and Copyright

Do not commit SQLite databases, exported CSV/JSON reports, or full article text. The repository should contain code, configuration, tests, and documentation only.

## Scraping Etiquette

Keep defaults conservative:

- identifiable user-agent;
- low per-domain concurrency;
- delays between requests;
- retries with backoff;
- robots.txt respected when possible.
