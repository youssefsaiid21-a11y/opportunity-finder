# Opportunity Finder

A Python pipeline that searches public sources, crawls candidate pages, classifies and scores opportunities, deduplicates results, and formats a digest for Microsoft Teams.

**Status: integration prototype.** This portfolio version uses a fictional organization configuration. No customer records, recipient identities, organization strategy or live credentials are included in the current configuration.

## Pipeline

`search → crawl → classify → score → deduplicate → format → deliver`

Async clients, configurable concurrency, SQLite history, rule-based classification fallbacks, LLM prompts and structured result models live in separate modules under `steps/`, `clients.py`, `models.py` and `db.py`.

## Local setup

Use Python 3.11 or later and an isolated environment:

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
cp .env.example .env
cp config.example.json config.json
```

Set your own development API keys in `.env`; review the example configuration and prompts before running. The classification rules use an illustrative industrial safety domain and should be adapted to your use case. The configured model is an existing project default; provider access and availability must be checked separately.

```sh
python main.py --config config.json --run-now
```

This command performs external searches, invokes paid APIs when configured, crawls websites and may post to the configured Teams webhook. Use a dedicated test channel. Omitting `--run-now` starts the repeating scheduler.

## Review scope

Start with `pipeline.py`, then `steps/` and `config.py`. The portfolio cleanup checks syntax and configuration structure; it does not establish live provider compatibility, opportunity accuracy or production readiness. No live digest is sent as part of repository preparation.

Local `.env`, `config.json`, databases and generated digest files are ignored. Keep business configuration and recipient identities outside Git.
