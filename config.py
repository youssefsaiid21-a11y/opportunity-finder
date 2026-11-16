"""Configuration loader — config.json + .env resolution."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

_PLACEHOLDER = re.compile(r"\$\{([^}]+)\}")


def _resolve(value, env: dict[str, str]):
    """Recursively replace ${VAR} with env values."""
    if isinstance(value, str):
        return _PLACEHOLDER.sub(lambda m: env.get(m.group(1), m.group(0)), value)
    if isinstance(value, dict):
        return {k: _resolve(v, env) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve(v, env) for v in value]
    return value


@dataclass
class AppConfig:
    # Search dimensions
    target_geographies: list[str] = field(default_factory=list)
    target_industries: list[str] = field(default_factory=list)
    target_categories: list[str] = field(default_factory=list)
    multilingual_keywords: dict[str, list[str]] = field(default_factory=dict)

    # Credentials
    anthropic_api_key: str = ""
    serper_api_key: str = ""
    google_cse_api_key: str = ""
    google_cse_id: str = ""
    teams_webhook_url: str = ""

    # LLM
    llm_model: str = "claude-sonnet-4-20250514"
    llm_max_tokens: int = 4096
    llm_temperature: float = 0.1
    llm_batch_size: int = 10

    # Search
    serper_results_per_query: int = 5
    serper_concurrency: int = 5
    search_enabled: bool = True

    # Browser
    browser_concurrency: int = 10
    browser_timeout_ms: int = 10000
    max_crawl_urls: int = 150
    browser_enabled: bool = True
    pdf_enabled: bool = False

    # Pipeline
    scrape_frequency_days: int = 7
    history_retention_days: int = 180
    scoring_threshold: int = 4
    fuzzy_dedupe_threshold: int = 85
    result_limits: dict[str, int] = field(
        default_factory=lambda: {"conference": 15, "innovation_challenge": 15, "tender": 20}
    )
    db_path: str = "opportunity_finder.db"
    fallback_dir: str = "."
    log_level: str = "INFO"

    # Notifications
    teams_mention_recipients: list[dict[str, str]] = field(default_factory=list)

    # Example Company profile
    organization_profile: dict = field(default_factory=dict)
    excluded_keywords: list[str] = field(default_factory=list)


def load_config(path: str = "config.json") -> AppConfig:
    """Load config.json, resolve ${VAR} placeholders from .env, validate."""
    load_dotenv()
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"Config not found: {config_path.resolve()}")

    raw = json.loads(config_path.read_text(encoding="utf-8"))
    resolved = _resolve(raw, dict(os.environ))

    cfg = AppConfig(**{
        k: resolved[k] for k in resolved
        if k in {f.name for f in AppConfig.__dataclass_fields__.values()}
    })

    if not cfg.anthropic_api_key:
        raise ValueError("ANTHROPIC_API_KEY not set")
    if not cfg.serper_api_key:
        raise ValueError("SERPER_API_KEY not set")
    return cfg
