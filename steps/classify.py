"""Step 3: Classify opportunities using LLM with rule-based fallback."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from clients import LLMClient
from config import AppConfig
from models import Opportunity, parse_datetime, strip_code_fences

logger = logging.getLogger(__name__)

_PROMPT: str | None = None

_HARD_EXCLUDE = [
    "hackathon", "cybersecurity only", "pre-seed only", "pre-product only",
    "b2c", "consumer app", "academic research only", "purely academic",
]

# Strong signal: direct match to Example Company's product domain
_POSITIVE_STRONG = [
    "video analytics", "computer vision", "cctv analytics", "ppe detection",
    "safety monitoring", "site monitoring", "perimeter monitoring",
    "incident detection", "behavioural analytics", "behavior analytics",
    "hard hat detection", "protective equipment detection",
    "ai surveillance", "smart cctv", "real-time alerting",
]

# Moderate signal: industry/event context that is likely relevant
_POSITIVE_MODERATE = [
    "safety", "hse", "ehs", "occupational health", "workplace safety",
    "construction safety", "industrial safety", "safety compliance",
    "ppe", "hard hat", "protective equipment", "safety officer",
    "safety conference", "safety summit", "safety expo",
    "tender", "rfp", "rfq", "procurement", "bid notice",
]

# High-value source domains — results from these get auto-promoted to LLM review
_HIGH_VALUE_DOMAINS = {
    # Government procurement portals
    "sam.gov", "ted.europa.eu", "ungm.org", "gebiz.gov.sg", "etimad.sa",
    "tejari.com", "contracts.gov.uk", "tenders.nsw.gov.au",
    # Event platforms
    "10times.com", "eventbrite.com", "events.com", "conference-service.com",
    # Industry-specific
    "procurementleaders.com", "safetyandhealthmagazine.com",
    "ohsonline.com", "ishn.com", "ehstoday.com",
}

# Combined for backward compat with rule_classify
_POSITIVE = _POSITIVE_STRONG + _POSITIVE_MODERATE


def _load_prompt() -> str:
    global _PROMPT
    if _PROMPT is None:
        _PROMPT = (Path(__file__).parent.parent / "prompts" / "classify.md").read_text(encoding="utf-8")
    return _PROMPT


def _is_high_value_source(opp: Opportunity) -> bool:
    """Check if the result comes from a known high-value domain."""
    domain = (opp.source or "").lower().removeprefix("www.")
    return any(domain == d or domain.endswith("." + d) for d in _HIGH_VALUE_DOMAINS)


async def run(opportunities: list[Opportunity], config: AppConfig, llm: LLMClient) -> list[Opportunity]:
    """Classify all opportunities. Uses LLM for promising items, rules for the rest.

    Classification strategy (expert approach):
    1. High-value source domains → always send to LLM (tender portals, event sites)
    2. Strong product signal → always send to LLM (video analytics, PPE detection, etc.)
    3. Moderate industry signal without excludes → send to LLM
    4. No signal or hard-exclude → rule-based reject
    """
    llm_candidates = []
    for opp in opportunities:
        text = (opp.title + " " + opp.raw_text[:500]).lower()
        has_exclude = any(kw in text for kw in _HARD_EXCLUDE)

        if has_exclude:
            _rule_classify(opp)
            continue

        # High-value sources always get LLM review
        if _is_high_value_source(opp):
            llm_candidates.append(opp)
            continue

        # Strong product signal → LLM
        has_strong = any(kw in text for kw in _POSITIVE_STRONG)
        if has_strong:
            llm_candidates.append(opp)
            continue

        # Moderate signal → LLM
        has_moderate = any(kw in text for kw in _POSITIVE_MODERATE)
        if has_moderate:
            llm_candidates.append(opp)
            continue

        _rule_classify(opp)

    logger.info("Classify: %d for LLM, %d rule-based", len(llm_candidates), len(opportunities) - len(llm_candidates))

    # Process LLM candidates in batches
    sem = asyncio.Semaphore(5)
    batches = [llm_candidates[i:i + config.llm_batch_size]
               for i in range(0, len(llm_candidates), config.llm_batch_size)]

    async def _do_batch(batch):
        async with sem:
            await _classify_batch(batch, config, llm)

    await asyncio.gather(*[_do_batch(b) for b in batches])

    eligible = sum(1 for o in opportunities if o.eligibility == "eligible")
    ineligible = sum(1 for o in opportunities if o.eligibility == "ineligible")
    unclear = sum(1 for o in opportunities if o.eligibility == "unclear")
    logger.info("Classify complete: %d eligible, %d ineligible, %d unclear", eligible, ineligible, unclear)

    # Dump filtered items for review
    _dump_filtered(opportunities)

    return opportunities


async def _classify_batch(batch: list[Opportunity], config: AppConfig, llm: LLMClient) -> None:
    """Classify a batch via LLM. Falls back to rules on failure."""
    prompt = _load_prompt()
    user_content = json.dumps([{
        "raw_id": o.id, "title": o.title, "url": o.url,
        "source_name": o.source, "source_language": o.language,
        "raw_text": o.raw_text[:3000], "search_query_used": o.search_query,
    } for o in batch], ensure_ascii=False)

    try:
        resp = await llm.complete(prompt, user_content, config.llm_model,
                                  config.llm_max_tokens, config.llm_temperature)
        results = json.loads(strip_code_fences(resp))
        if not isinstance(results, list) or len(results) != len(batch):
            raise ValueError(f"LLM returned {len(results)} results for {len(batch)} inputs")

        for opp, r in zip(batch, results):
            _apply_llm_result(opp, r)

    except Exception as exc:
        logger.warning("LLM classify failed, using rules: %s", exc)
        for opp in batch:
            _rule_classify(opp)


def _apply_llm_result(opp: Opportunity, r: dict) -> None:
    elig = r.get("eligibility", "unclear")
    if elig == "irrelevant":
        elig = "ineligible"
    opp.eligibility = elig
    opp.category = r.get("category", "irrelevant")
    opp.ineligibility_reason = r.get("ineligibility_reason")
    opp.industries = r.get("industries_matched") or []
    opp.geographies = r.get("geography_matched") or []
    opp.deadline = parse_datetime(r.get("deadline_or_date"))
    opp.organizer = r.get("organizer_or_issuer")
    opp.is_expired = bool(r.get("is_expired", False))
    opp.notes = r.get("lawyer_notes", "")
    opp.review_question = r.get("question") if elig == "unclear" else None
    opp.classify_method = "llm"


def _rule_classify(opp: Opportunity) -> None:
    """Conservative rule-based classification."""
    text = (opp.title + " " + opp.raw_text[:2000]).lower()

    for kw in _HARD_EXCLUDE:
        if kw in text:
            opp.eligibility = "ineligible"
            opp.category = "irrelevant"
            opp.ineligibility_reason = f"Hard-exclude keyword: '{kw}'"
            opp.classify_method = "rule"
            return

    if any(kw in text for kw in _POSITIVE):
        opp.eligibility = "unclear"
        opp.category = _infer_category(text)
        opp.notes = "Rule-based: positive signal, eligibility undetermined"
        opp.review_question = "Could not auto-classify — manual review needed."
        opp.classify_method = "rule"
        return

    opp.eligibility = "ineligible"
    opp.category = "irrelevant"
    opp.ineligibility_reason = "No relevant signal"
    opp.classify_method = "rule"


def _dump_filtered(opportunities: list[Opportunity]) -> None:
    """Write classify_debug.json with all items and their classification reasons."""
    out = []
    for o in opportunities:
        out.append({
            "title": o.title,
            "url": o.url,
            "eligibility": o.eligibility,
            "category": o.category,
            "reason": o.ineligibility_reason or o.notes or "",
            "method": o.classify_method,
            "search_query": o.search_query,
            "snippet": (o.snippet or "")[:200],
        })
    path = Path(__file__).parent.parent / "classify_debug.json"
    path.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info("Wrote classify debug to %s (%d items)", path.name, len(out))


def _infer_category(text: str) -> str:
    if any(kw in text for kw in ["tender", "rfp", "rfq", "bid", "procurement"]):
        return "tender"
    if any(kw in text for kw in ["challenge", "accelerator", "innovation"]):
        return "innovation_challenge"
    if any(kw in text for kw in ["conference", "summit", "expo", "forum"]):
        return "conference"
    return "irrelevant"
