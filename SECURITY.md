# Security Policy

## Secrets

Never commit `.env`, API keys, SQLite databases, exported CSV/JSON files, logs, or downloaded article text.

This repository intentionally ignores:

- `.env` and local environment variants;
- `data/`, including SQLite databases;
- `exports/`, including reports, audits, validation samples, and CSV/JSON output;
- virtual environments and Python build artifacts.

If a Pangram key is accidentally committed, revoke it in Pangram immediately and remove it from Git history before publishing the repository.

## Data Sensitivity

SQLite databases and exports can contain full article text, URLs, metadata, Pangram responses, and operational logs. Treat them as local research artifacts. Do not publish them unless you have the rights and consent needed to redistribute that content.

## Reporting Issues

For public projects, report vulnerabilities through GitHub Security Advisories when available. If advisories are not enabled, open a minimal issue that does not include secrets, API keys, private datasets, or full article text.

## Responsible Use

Use identifiable user agents, conservative rate limits, and respect robots.txt where possible. This project is designed for transparency and reproducible research, not aggressive scraping.
