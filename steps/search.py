"""Step 1: Generate queries and execute searches (Serper → Brave → DuckDuckGo fallback)."""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from urllib.parse import urlparse

from clients import SerperClient, GoogleCSEClient, DuckDuckGoClient
from config import AppConfig
from models import Opportunity, classify_tier, extract_domain

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Query templates — quoted phrases for precision
# ---------------------------------------------------------------------------

# Global queries (no geography, English only)
_GLOBAL_QUERIES = [
    # Conferences & expos — consolidated
    '"safety conference" OR "safety summit" OR "HSE conference" {yr}',
    '"industrial safety" OR "construction safety" conference OR expo {yr}',
    '"video analytics" OR "computer vision" conference safety {yr}',
    '"oil and gas" OR "mining" safety conference OR summit {yr}',
    # Targeted site searches for events (Serper/Google only, filtered for DDG)
    'site:10times.com "safety" OR "HSE" OR "EHS" conference {yr}',
    'site:eventbrite.com "safety conference" OR "HSE summit" {yr}',
    # Innovation challenges — consolidated
    '"safety innovation" OR "construction technology" challenge OR competition {yr}',
    '"AI" OR "computer vision" startup challenge safety {yr}',
    # Tenders — consolidated
    '"video analytics" tender OR RFP OR procurement {yr}',
    '"PPE detection" OR "safety monitoring" tender OR RFP {yr}',
    '"CCTV analytics" OR "video surveillance" tender OR procurement {yr}',
    '"construction site monitoring" OR "site safety" tender OR procurement {yr}',
    # Tender portals (Serper/Google only, filtered for DDG)
    'site:sam.gov "video analytics" OR "PPE detection" OR "safety monitoring"',
    'site:ted.europa.eu "video analytics" OR "CCTV analytics" OR "safety monitoring"',
    'site:ungm.org "video analytics" OR "video surveillance" safety',
    'site:gebiz.gov.sg "video analytics" OR "safety monitoring"',
    'site:etimad.sa "video analytics" OR "safety monitoring"',
    'site:find-tender.service.gov.uk "video analytics" OR "CCTV analytics"',
    'site:contractsfinder.service.gov.uk "video analytics" OR "PPE detection"',
    'site:tenders.gov.au "video analytics" OR "safety monitoring"',
    'site:eprocure.gov.in "video analytics" OR "safety monitoring"',
    'site:dgmarket.com "video analytics" OR "PPE detection"',
    'site:devbusiness.un.org "video analytics" OR "safety monitoring"',
]

# Geo-specific queries for priority markets — consolidated
_GEO_QUERIES = [
    '"safety" OR "HSE" conference OR summit OR expo {geo} {yr}',
    '"video analytics" OR "safety monitoring" tender OR RFP {geo}',
    '"safety innovation" OR "smart city" challenge OR pilot {geo} {yr}',
]

# ---------------------------------------------------------------------------
# Domain blocklist — noise sources that never yield real opportunities
# ---------------------------------------------------------------------------
_BLOCKED_DOMAINS = {
    # Chinese Q&A / encyclopedia
    "zhihu.com", "baidu.com", "zhidao.baidu.com", "baike.baidu.com",
    "sogou.com", "360.cn", "douban.com", "csdn.net", "jianshu.com",
    "bilibili.com", "163.com", "qq.com", "sohu.com",
    # Generic reference / non-opportunity
    "wikipedia.org", "investopedia.com", "dictionary.com", "merriam-webster.com",
    "cambridge.org", "quora.com", "wiktionary.org", "britannica.com",
    # Search engines (their own SERPs leak in via DDG fallback)
    "bing.com", "google.com", "duckduckgo.com", "yahoo.com",
    # Video / social / retail
    "youtube.com", "tiktok.com", "facebook.com", "instagram.com",
    "pinterest.com", "reddit.com", "twitter.com", "x.com",
    "amazon.com", "ebay.com", "alibaba.com", "thewarehouse.co.nz",
    # Tech support / forums / unrelated
    "support.google.com", "help.thewarehouse.co.nz", "sketchupcommunity.com",
    "forums.sketchup.com", "stackoverflow.com", "superuser.com",
    # Informational-only regulatory sites (no opportunities, just guidance)
    "osha.gov", "ilo.org", "hse.gov.uk",
    # News aggregators / price trackers
    "oilprice.com", "tradingview.com", "reuters.com",
}

# Quick check for non-Latin scripts that indicate non-English content
_NON_ENGLISH_RE = re.compile(r"[\u4e00-\u9fff\u3040-\u309f\u30a0-\u30ff\uac00-\ud7af]")


def _is_blocked(url: str) -> bool:
    """Check if a URL's domain is in the blocklist."""
    try:
        domain = urlparse(url).netloc.lower().removeprefix("www.")
        return any(domain == d or domain.endswith("." + d) for d in _BLOCKED_DOMAINS)
    except Exception:
        return False


def _looks_non_english(title: str, snippet: str) -> bool:
    """Reject results where the title or snippet is primarily non-English."""
    text = title + " " + snippet
    if not text.strip():
        return False
    non_eng_chars = len(_NON_ENGLISH_RE.findall(text))
    return non_eng_chars > len(text) * 0.15


def generate_queries(config: AppConfig) -> list[tuple[str, str, str]]:
    """Generate (query_text, geo, language) tuples.

    Produces global queries (no geography filter) plus geo-specific queries
    for each target geography. All queries are English-only.
    """
    queries: list[tuple[str, str, str]] = []
    yr = datetime.now(timezone.utc).year
    subs = {"yr": yr}

    # Global queries
    for tmpl in _GLOBAL_QUERIES:
        queries.append((tmpl.format(**subs), "global", "en"))

    # Geo-specific queries for priority markets
    for geo in config.target_geographies:
        for tmpl in _GEO_QUERIES:
            queries.append((tmpl.format(geo=geo, **subs), geo, "en"))

    # Deduplicate while preserving order
    seen = set()
    unique = []
    for q in queries:
        if q[0] not in seen:
            seen.add(q[0])
            unique.append(q)
    queries = unique

    logger.info("Generated %d search queries", len(queries))
    return queries


async def _probe_serper(serper: SerperClient) -> bool:
    """Send a single test query to check if Serper has credits."""
    results = await serper.search("test", num=1)
    return len(results) > 0


async def _execute_queries(client, queries, results_per_query, batch_size=10, delay=1.0):
    """Run queries against a search client (Serper or DuckDuckGo)."""
    opportunities: list[Opportunity] = []
    blocked = 0
    non_eng = 0
    for i in range(0, len(queries), batch_size):
        batch = queries[i:i + batch_size]
        tasks = [client.search(q, results_per_query) for q, _, _ in batch]
        results = await asyncio.gather(*tasks)

        for (query_text, geo, lang), search_results in zip(batch, results):
            for sr in search_results:
                if not sr.url:
                    continue
                if _is_blocked(sr.url):
                    blocked += 1
                    continue
                if _looks_non_english(sr.title, sr.snippet):
                    non_eng += 1
                    continue
                opportunities.append(Opportunity(
                    title=sr.title or sr.snippet[:100],
                    url=sr.url,
                    source=sr.source or extract_domain(sr.url),
                    source_tier=classify_tier(sr.url),
                    language="en",
                    snippet=sr.snippet,
                    raw_text=sr.snippet,
                    search_query=query_text,
                ))

        if i + batch_size < len(queries):
            await asyncio.sleep(delay)

    if blocked or non_eng:
        logger.info("Pre-filtered: %d blocked domains, %d non-English results", blocked, non_eng)

    # Deduplicate by URL (keep first seen, which came from the highest-priority query)
    seen_urls = set()
    unique_opps = []
    for opp in opportunities:
        norm = opp.url.lower().rstrip("/")
        if norm not in seen_urls:
            seen_urls.add(norm)
            unique_opps.append(opp)
    if len(unique_opps) < len(opportunities):
        logger.info("URL dedup: %d → %d unique results", len(opportunities), len(unique_opps))
    return unique_opps


async def _probe_google_cse(google: GoogleCSEClient) -> bool:
    """Send a single test query to check if Google CSE is working."""
    results = await google.search("test", num=1)
    return len(results) > 0


def _filter_ddg_queries(queries: list[tuple[str, str, str]]) -> list[tuple[str, str, str]]:
    """Remove site:-specific queries that don't work well on DuckDuckGo."""
    filtered = [q for q in queries if not q[0].strip().startswith("site:")]
    removed = len(queries) - len(filtered)
    if removed:
        logger.info("Removed %d site:-specific queries for DDG (unsupported)", removed)
    return filtered


async def _run_ddg(queries: list[tuple[str, str, str]], results_per_query: int) -> list[Opportunity]:
    """Run queries via DuckDuckGo with DDG-friendly settings."""
    ddg_queries = _filter_ddg_queries(queries)
    logger.info("Using DuckDuckGo for %d queries (5s gap, sequential)", len(ddg_queries))
    async with DuckDuckGoClient(concurrency=1) as ddg:
        return await _execute_queries(
            ddg, ddg_queries, results_per_query,
            batch_size=1, delay=0)  # delay handled inside DuckDuckGoClient


async def run(config: AppConfig, serper: SerperClient) -> list[Opportunity]:
    """Execute all searches. Serper → Google CSE (if configured) → DuckDuckGo."""
    queries = generate_queries(config)

    # Probe Serper first
    serper_ok = await _probe_serper(serper)
    if serper_ok:
        logger.info("Serper OK — using Serper for %d queries", len(queries))
        opportunities = await _execute_queries(
            serper, queries, config.serper_results_per_query)
    elif config.google_cse_api_key and config.google_cse_id:
        logger.warning("Serper unavailable — trying Google Custom Search")
        async with GoogleCSEClient(
            api_key=config.google_cse_api_key,
            cse_id=config.google_cse_id,
            concurrency=5,
        ) as google:
            google_ok = await _probe_google_cse(google)
            if google_ok:
                logger.info("Google CSE OK — using Google for %d queries", len(queries))
                opportunities = await _execute_queries(
                    google, queries, config.serper_results_per_query,
                    batch_size=5, delay=0.5)
            else:
                logger.warning("Google CSE unavailable — falling back to DuckDuckGo")
                opportunities = await _run_ddg(queries, config.serper_results_per_query)
    else:
        logger.warning("Serper unavailable (no credits?) — falling back to DuckDuckGo")
        opportunities = await _run_ddg(queries, config.serper_results_per_query)

    logger.info("Search complete: %d results from %d queries", len(opportunities), len(queries))
    return opportunities
