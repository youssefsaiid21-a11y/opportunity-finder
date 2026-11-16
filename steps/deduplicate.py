"""Step 5: Fingerprint dedup against history + fuzzy title dedup."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import aiosqlite
from rapidfuzz import fuzz

from config import AppConfig
from models import Opportunity, compute_fingerprint, normalise_url
import db

logger = logging.getLogger(__name__)


async def run(
    opportunities: list[Opportunity], config: AppConfig, conn: aiosqlite.Connection
) -> tuple[list[Opportunity], list[Opportunity]]:
    """
    Deduplicate and split into digest_queue and review_queue.

    Returns: (digest_queue, review_queue)
    """
    # Filter out ineligible
    eligible = [o for o in opportunities if o.eligibility == "eligible"]
    unclear = [o for o in opportunities if o.eligibility == "unclear"]

    eligible_deduped = await _deduplicate(eligible, config, conn)
    unclear_deduped = await _deduplicate(unclear, config, conn)

    # Split by scoring threshold
    digest = [o for o in eligible_deduped if (o.score or 0) >= config.scoring_threshold]
    below = [o for o in eligible_deduped if (o.score or 0) < config.scoring_threshold]
    review = unclear_deduped + below

    # Apply per-category limits to digest
    digest = _apply_limits(digest, config)
    review = review[:50]  # cap review queue

    logger.info("Dedup complete: %d digest, %d review", len(digest), len(review))
    return digest, review


async def _deduplicate(
    items: list[Opportunity], config: AppConfig, conn: aiosqlite.Connection
) -> list[Opportunity]:
    """Remove duplicates via fingerprint (history + in-run) and fuzzy title matching."""
    result = []
    seen_fps: set[str] = set()
    seen_titles: list[tuple[str, Opportunity]] = []

    for opp in items:
        url = normalise_url(opp.url)
        fp = compute_fingerprint(opp.url, opp.title, opp.deadline)
        opp.fingerprint = fp

        # Check history
        if await db.exists(conn, fp):
            opp.is_duplicate = True
            continue

        # Check in-run exact
        if fp in seen_fps:
            opp.is_duplicate = True
            continue

        # Check fuzzy title
        norm_title = opp.title.lower().strip()
        dup = False
        for existing_title, existing_opp in seen_titles:
            if fuzz.token_sort_ratio(norm_title, existing_title) >= config.fuzzy_dedupe_threshold:
                if (opp.score or 0) > (existing_opp.score or 0):
                    # Replace with higher-scoring one
                    seen_titles.remove((existing_title, existing_opp))
                    result.remove(existing_opp)
                    seen_fps.discard(existing_opp.fingerprint or "")
                    break
                else:
                    dup = True
                    break

        if not dup:
            seen_fps.add(fp)
            seen_titles.append((norm_title, opp))
            result.append(opp)

    return result


def _apply_limits(items: list[Opportunity], config: AppConfig) -> list[Opportunity]:
    """Sort by score desc, apply per-category limits."""
    far_future = datetime(2099, 1, 1, tzinfo=timezone.utc)
    items.sort(key=lambda o: (-(o.score or 0), o.deadline or far_future))

    by_cat: dict[str, list[Opportunity]] = {}
    for o in items:
        by_cat.setdefault(o.category or "other", []).append(o)

    result = []
    for cat, cat_items in by_cat.items():
        limit = config.result_limits.get(cat, 15)
        result.extend(cat_items[:limit])

    result.sort(key=lambda o: (-(o.score or 0), o.deadline or far_future))
    return result
