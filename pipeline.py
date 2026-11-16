"""Pipeline orchestrator — runs all steps in sequence."""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone

import aiosqlite

from config import AppConfig
from clients import SerperClient, Browser, LLMClient
from models import DigestResult
from steps import search, crawl, classify, score, deduplicate, format as fmt
import db

logger = logging.getLogger(__name__)

_TIMEOUT = {"search": 1200, "crawl": 600, "classify": 600, "score": 600, "dedup": 30, "format": 60}


async def run(config: AppConfig) -> DigestResult:
    """Execute the full pipeline: search → crawl → classify → score → dedup → format."""
    run_id = uuid.uuid4().hex
    started = datetime.now(timezone.utc)
    logger.info("[%s] Pipeline started", run_id)

    try:
        async with aiosqlite.connect(config.db_path) as conn:
            await conn.execute("PRAGMA journal_mode=WAL")
            await db.init(conn)
            if config.history_retention_days > 0:
                await db.prune(conn, config.history_retention_days)

            async with SerperClient(config.serper_api_key, config.serper_concurrency) as serper:
                # Step 1: Search
                logger.info("[%s] SEARCH...", run_id)
                opps = await asyncio.wait_for(
                    search.run(config, serper), timeout=_TIMEOUT["search"])
                logger.info("[%s] SEARCH: %d results", run_id, len(opps))

            if not opps:
                logger.warning("[%s] No results — ending early", run_id)
                return DigestResult(run_id=run_id, status="no_results")

            # Step 2: Crawl — start browser only when needed
            async with Browser(config.browser_concurrency) as browser:
                logger.info("[%s] CRAWL...", run_id)
                opps = await asyncio.wait_for(
                    crawl.run(opps, config, browser), timeout=_TIMEOUT["crawl"])
                logger.info("[%s] CRAWL done", run_id)

            async with LLMClient(config.anthropic_api_key) as llm:
                # Step 3: Classify
                logger.info("[%s] CLASSIFY...", run_id)
                opps = await asyncio.wait_for(
                    classify.run(opps, config, llm), timeout=_TIMEOUT["classify"])
                logger.info("[%s] CLASSIFY done", run_id)

                # Step 4: Score
                logger.info("[%s] SCORE...", run_id)
                opps = await asyncio.wait_for(
                    score.run(opps, config, llm), timeout=_TIMEOUT["score"])
                logger.info("[%s] SCORE done", run_id)

            # Step 5: Deduplicate
            logger.info("[%s] DEDUP...", run_id)
            digest, review = await asyncio.wait_for(
                deduplicate.run(opps, config, conn), timeout=_TIMEOUT["dedup"])
            logger.info("[%s] DEDUP: %d digest, %d review", run_id, len(digest), len(review))

            # Step 6: Format & Send
            logger.info("[%s] FORMAT...", run_id)
            result = await asyncio.wait_for(
                fmt.run(digest, review, config, conn, run_id), timeout=_TIMEOUT["format"])

            duration = (datetime.now(timezone.utc) - started).total_seconds()
            logger.info("[%s] Pipeline completed in %.1fs — status=%s", run_id, duration, result.status)
            return result

    except asyncio.TimeoutError as exc:
        logger.error("[%s] Pipeline timed out: %s", run_id, exc)
        return DigestResult(run_id=run_id, status="failed")
    except Exception as exc:
        logger.error("[%s] Pipeline failed: %s", run_id, exc, exc_info=True)
        return DigestResult(run_id=run_id, status="failed")
