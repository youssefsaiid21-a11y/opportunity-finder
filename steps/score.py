"""Step 4: Score opportunities using LLM with heuristic fallback."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from clients import LLMClient
from config import AppConfig
from models import Opportunity, strip_code_fences

logger = logging.getLogger(__name__)

_PROMPT: str | None = None

_SCORE_MAXIMA = {
    "geography_match": 2.0, "industry_match": 2.0, "use_case_directness": 2.5,
    "buyer_relevance": 1.5, "source_quality": 1.0, "freshness_and_urgency": 1.0,
}


def _load_prompt() -> str:
    global _PROMPT
    if _PROMPT is None:
        _PROMPT = (Path(__file__).parent.parent / "prompts" / "score.md").read_text(encoding="utf-8")
    return _PROMPT


def _to_priority(score: int) -> str:
    if score >= 8: return "High"
    if score >= 5: return "Medium"
    return "Low"


async def run(opportunities: list[Opportunity], config: AppConfig, llm: LLMClient) -> list[Opportunity]:
    """Score eligible/unclear opportunities via LLM. Skip ineligible."""
    to_score = [o for o in opportunities if o.eligibility != "ineligible"]
    for o in opportunities:
        if o.eligibility == "ineligible":
            o.score, o.priority = 1, "Low"

    logger.info("Score: %d ineligible (instant), %d for LLM", len(opportunities) - len(to_score), len(to_score))

    sem = asyncio.Semaphore(5)
    batches = [to_score[i:i + config.llm_batch_size]
               for i in range(0, len(to_score), config.llm_batch_size)]

    async def _do_batch(batch):
        async with sem:
            await _score_batch(batch, config, llm)

    await asyncio.gather(*[_do_batch(b) for b in batches])

    high = sum(1 for o in opportunities if o.priority == "High")
    logger.info("Score complete: %d scored (%d High priority)", len(opportunities), high)
    return opportunities


async def _score_batch(batch: list[Opportunity], config: AppConfig, llm: LLMClient) -> None:
    prompt = _load_prompt()
    user_content = json.dumps([{
        "raw_id": o.id, "title": o.title, "url": o.url,
        "category": o.category, "eligibility": o.eligibility,
        "industries_matched": o.industries, "geography_matched": o.geographies,
        "deadline_or_date": o.deadline.isoformat() if o.deadline else None,
        "organizer_or_issuer": o.organizer, "source_tier": o.source_tier,
        "lawyer_notes": o.notes, "raw_text_excerpt": o.raw_text[:2000],
    } for o in batch], ensure_ascii=False)

    try:
        resp = await llm.complete(prompt, user_content, config.llm_model,
                                  config.llm_max_tokens, config.llm_temperature)
        results = json.loads(strip_code_fences(resp))
        if not isinstance(results, list) or len(results) != len(batch):
            raise ValueError(f"LLM returned {len(results)} for {len(batch)}")

        for opp, r in zip(batch, results):
            _apply_llm_score(opp, r)

    except Exception as exc:
        logger.warning("LLM score failed, using heuristic: %s", exc)
        for opp in batch:
            _heuristic_score(opp)


def _apply_llm_score(opp: Opportunity, r: dict) -> None:
    breakdown = r.get("scoring_breakdown", {})
    clamped = {}
    for dim, max_val in _SCORE_MAXIMA.items():
        try:
            clamped[dim] = min(float(breakdown.get(dim, 0)), max_val)
        except (ValueError, TypeError):
            clamped[dim] = 0.0

    total = max(1, min(10, round(sum(clamped.values()))))
    opp.score = total
    opp.priority = _to_priority(total)
    opp.score_breakdown = clamped


def _heuristic_score(opp: Opportunity) -> None:
    text = (opp.title + " " + opp.raw_text[:2000]).lower()
    geo = 2.0 if opp.geographies else 0.5
    ind = 2.0 if opp.industries else 0.5
    uc = sum(0.8 for kw in ["ppe", "video analytics", "safety monitoring", "computer vision"]
             if kw in text)
    uc = min(2.5, uc)
    buyer = 0.8 if any(b in text for b in ["hse", "safety officer", "operations"]) else 0.4
    src = {1: 1.0, 2: 0.6, 3: 0.3}.get(opp.source_tier, 0.3)
    urg = 0.7
    total = max(1, min(10, round(geo + ind + uc + buyer + src + urg)))
    opp.score = total
    opp.priority = _to_priority(total)
    opp.score_breakdown = {"geography_match": geo, "industry_match": ind,
                           "use_case_directness": uc, "buyer_relevance": buyer,
                           "source_quality": src, "freshness_and_urgency": urg}
