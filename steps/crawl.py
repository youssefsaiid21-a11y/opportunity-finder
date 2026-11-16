"""Step 2: Crawl detail pages with Playwright to enrich raw_text."""

from __future__ import annotations

import asyncio
import logging

from clients import Browser
from config import AppConfig
from models import Opportunity

logger = logging.getLogger(__name__)

_TEXT_LIMIT = 8000


async def run(opportunities: list[Opportunity], config: AppConfig, browser: Browser) -> list[Opportunity]:
    """Crawl top URLs (tier-1 first, up to budget) and enrich raw_text."""
    if not config.browser_enabled:
        logger.info("Browser disabled — skipping crawl")
        return opportunities

    # Sort: tier-1 first
    sorted_opps = sorted(opportunities, key=lambda o: o.source_tier)
    budget = config.max_crawl_urls

    async def _crawl_one(opp: Opportunity, skip: bool) -> None:
        if skip:
            return
        try:
            page = await asyncio.wait_for(
                browser.fetch(opp.url, timeout_ms=config.browser_timeout_ms),
                timeout=config.browser_timeout_ms / 1000 + 3,
            )
        except asyncio.TimeoutError:
            return

        if page.error:
            return
        if page.text:
            opp.raw_text = page.text[:_TEXT_LIMIT]
        if page.title and not opp.title:
            opp.title = page.title

        # PDF extraction (first link only)
        if config.pdf_enabled and page.pdf_links:
            pdf_text = await browser.extract_pdf(page.pdf_links[0])
            if pdf_text:
                opp.raw_text = pdf_text[:_TEXT_LIMIT]

    tasks = [_crawl_one(opp, i >= budget) for i, opp in enumerate(sorted_opps)]
    await asyncio.gather(*tasks, return_exceptions=True)

    crawled = min(len(sorted_opps), budget)
    logger.info("Crawl complete: %d crawled, %d snippet-only", crawled, max(0, len(sorted_opps) - budget))
    return opportunities  # return original list (mutated in-place)
