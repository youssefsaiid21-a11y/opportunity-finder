"""Step 6: Format digest, send to Teams, write history."""

from __future__ import annotations

import json
import logging
from datetime import datetime, date, timezone
from pathlib import Path

import httpx
import aiosqlite
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_fixed, RetryError

from config import AppConfig
from models import Opportunity, DigestResult
import db

logger = logging.getLogger(__name__)

_HEADERS = {
    "conference": "CONFERENCES / EVENTS",
    "innovation_challenge": "CORPORATE INNOVATION CHALLENGES",
    "tender": "TENDERS / PROCUREMENT NOTICES",
}
_TEAMS_MAX_BYTES = 27_000


async def run(
    digest: list[Opportunity], review: list[Opportunity],
    config: AppConfig, conn: aiosqlite.Connection, run_id: str,
) -> DigestResult:
    """Format, send to Teams, write history."""
    result = DigestResult(run_id=run_id, items_found=len(digest) + len(review),
                          items_posted=len(digest), items_review=len(review))

    if not digest and not review:
        result.status = "no_results"
        return result

    md = _build_markdown(digest, review, config)
    card = _build_card(digest, review, config)
    status = await _send_teams(md, card, config.teams_webhook_url, config.fallback_dir,
                               config.teams_mention_recipients)
    result.status = status

    if status == "fallback_file":
        result.fallback_path = f"digest_fallback_{date.today().isoformat()}.txt"

    if status in ("sent", "fallback_file"):
        posted_at = datetime.now(timezone.utc) if status == "sent" else None
        for opp in digest:
            await db.upsert(
                conn, opp.fingerprint or "", opp.url, opp.title,
                opp.category or "", ", ".join(opp.geographies),
                opp.organizer, opp.deadline, opp.score or 0,
                posted_at, opp.language, (opp.notes or "")[:500],
            )

    logger.info("Format complete: status=%s, posted=%d", status, len(digest))
    return result


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------

def _fmt_date(o: Opportunity) -> str:
    if not o.deadline:
        return "date TBC"
    label = "deadline" if o.category == "tender" else "due"
    return f"{label} {o.deadline.day} {o.deadline.strftime('%b %Y')}"


def _fmt_geo(o: Opportunity) -> str:
    return ", ".join(o.geographies) if o.geographies else "global"


def _build_markdown(digest: list[Opportunity], review: list[Opportunity], config: AppConfig) -> str:
    dt = datetime.now(timezone.utc)
    total = len(digest) + len(review)
    lines = [
        f"**Example Company Market Intelligence Digest** — {dt.day} {dt.strftime('%B %Y')}",
        f"Items: {total} (New: {len(digest)} | Review: {len(review)})",
        "",
    ]

    by_cat: dict[str, list[Opportunity]] = {}
    for o in digest:
        by_cat.setdefault(o.category or "other", []).append(o)

    for cat in ["conference", "innovation_challenge", "tender"]:
        items = by_cat.get(cat, [])
        if not items:
            continue
        lines.append(f"**{_HEADERS.get(cat, cat.upper())} ({len(items)})**")
        for o in items:
            lines.append(f"[{o.score or 0}] {o.title} | {_fmt_geo(o)} | {_fmt_date(o)} | {o.url}")
        lines.append("")

    if review:
        lines.append(f"**NEEDS REVIEW ({len(review)})**")
        for o in review:
            q = o.review_question or "eligibility unclear"
            lines.append(f"[{o.score or 0}] {o.title} | {_fmt_geo(o)} | {q} | {o.url}")
        lines.append("")

    # Add @mentions
    if config.teams_mention_recipients:
        tags = " ".join(f"<at>{r['name']}</at>" for r in config.teams_mention_recipients)
        lines.append(f"cc {tags}")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Adaptive Card
# ---------------------------------------------------------------------------

def _build_card(digest: list[Opportunity], review: list[Opportunity], config: AppConfig) -> dict | None:
    try:
        dt = datetime.now(timezone.utc)
        total = len(digest) + len(review)
        body = [
            {"type": "TextBlock", "text": "Example Company Market Intelligence Digest",
             "weight": "Bolder", "size": "Large", "wrap": True},
            {"type": "TextBlock", "wrap": True, "isSubtle": True,
             "text": f"{dt.day} {dt.strftime('%B %Y')} | Items: {total} (New: {len(digest)} | Review: {len(review)})"},
        ]

        by_cat: dict[str, list[Opportunity]] = {}
        for o in digest:
            by_cat.setdefault(o.category or "other", []).append(o)

        for cat in ["conference", "innovation_challenge", "tender"]:
            items = by_cat.get(cat, [])
            if not items:
                continue
            body.append({"type": "TextBlock", "text": f"{_HEADERS.get(cat, cat)} ({len(items)})",
                         "weight": "Bolder", "separator": True, "wrap": True})
            for o in items:
                body.append({"type": "TextBlock", "wrap": True, "spacing": "Small",
                             "text": f"[{o.score or 0}] {o.title} | {_fmt_geo(o)} | {_fmt_date(o)} | [link]({o.url})"})

        if review:
            body.append({"type": "TextBlock", "text": f"NEEDS REVIEW ({len(review)})",
                         "weight": "Bolder", "separator": True, "wrap": True})
            for o in review:
                q = o.review_question or "eligibility unclear"
                body.append({"type": "TextBlock", "wrap": True, "spacing": "Small",
                             "text": f"[{o.score or 0}] {o.title} | {_fmt_geo(o)} | {q} | [link]({o.url})"})

        # Add @mentions
        entities = []
        if config.teams_mention_recipients:
            tags = " ".join(f"<at>{r['name']}</at>" for r in config.teams_mention_recipients)
            body.append({"type": "TextBlock", "wrap": True, "separator": True,
                         "text": f"cc {tags}"})
            entities = [
                {"type": "mention", "text": f"<at>{r['name']}</at>",
                 "mentioned": {"id": r["email"], "name": r["name"]}}
                for r in config.teams_mention_recipients
            ]

        card = {"$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                "type": "AdaptiveCard", "version": "1.4", "body": body}
        if entities:
            card["msteams"] = {"entities": entities}
        return card
    except Exception as exc:
        logger.warning("Card build failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Teams send
# ---------------------------------------------------------------------------

async def _send_teams(markdown: str, card: dict | None, webhook_url: str, fallback_dir: str,
                      mention_recipients: list[dict[str, str]] | None = None) -> str:
    if not webhook_url:
        _write_fallback(markdown, fallback_dir)
        return "fallback_file"

    async with httpx.AsyncClient(timeout=30.0) as client:
        # Try card first
        if card:
            payload = {"type": "message", "attachments": [
                {"contentType": "application/vnd.microsoft.card.adaptive", "content": card}
            ]}
            if len(json.dumps(payload).encode()) <= _TEAMS_MAX_BYTES:
                if await _try_post(client, payload, webhook_url):
                    return "sent"

        # Fallback to markdown — include mention entities so Teams resolves <at> tags
        md_payload: dict = {"text": markdown}
        if mention_recipients:
            md_payload["entities"] = [
                {"type": "mention", "text": f"<at>{r['name']}</at>",
                 "mentioned": {"id": r["email"], "name": r["name"]}}
                for r in mention_recipients
            ]
        if await _try_post(client, md_payload, webhook_url):
            return "sent"

    _write_fallback(markdown, fallback_dir)
    return "fallback_file"


async def _try_post(client: httpx.AsyncClient, payload: dict, url: str) -> bool:
    try:
        await _post_retry(client, payload, url)
        return True
    except Exception as exc:
        logger.error("Teams send failed: %s", exc)
        return False


@retry(stop=stop_after_attempt(3), wait=wait_fixed(2),
       retry=retry_if_exception_type(httpx.HTTPError), reraise=True)
async def _post_retry(client: httpx.AsyncClient, payload: dict, url: str) -> None:
    resp = await client.post(url, json=payload, headers={"Content-Type": "application/json"})
    if resp.status_code not in (200, 202):
        raise httpx.HTTPStatusError(f"HTTP {resp.status_code}", request=resp.request, response=resp)
    logger.info("Teams sent (HTTP %d)", resp.status_code)


def _write_fallback(text: str, fallback_dir: str) -> None:
    path = Path(fallback_dir) / f"digest_fallback_{date.today().isoformat()}.txt"
    try:
        path.write_text(text, encoding="utf-8")
        logger.info("Fallback written: %s", path)
    except Exception as exc:
        logger.error("Fallback write failed: %s", exc)
