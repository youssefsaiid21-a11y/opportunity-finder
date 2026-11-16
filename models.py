"""Single progressive Opportunity model + DigestResult."""

from __future__ import annotations

import hashlib
import re
import unicodedata
import uuid
from datetime import datetime, timezone
from typing import Literal
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

from pydantic import BaseModel, Field


class Opportunity(BaseModel):
    """Single model enriched progressively through the pipeline."""

    # -- Set by search --
    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    title: str
    url: str
    source: str = ""
    source_tier: Literal[1, 2, 3] = 3
    language: str = "en"
    snippet: str = ""
    raw_text: str = ""
    search_query: str = ""
    discovered_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    # -- Set by classify --
    category: str | None = None
    eligibility: str | None = None
    ineligibility_reason: str | None = None
    industries: list[str] = Field(default_factory=list)
    geographies: list[str] = Field(default_factory=list)
    deadline: datetime | None = None
    organizer: str | None = None
    is_expired: bool = False
    notes: str = ""
    review_question: str | None = None
    classify_method: str = "pending"

    # -- Set by score --
    score: int | None = None
    priority: str | None = None
    score_breakdown: dict[str, float] = Field(default_factory=dict)

    # -- Set by deduplicate --
    fingerprint: str | None = None
    is_duplicate: bool = False


class DigestResult(BaseModel):
    """Summary of a pipeline run."""
    run_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    status: Literal["sent", "failed", "fallback_file", "no_results"] = "failed"
    items_found: int = 0
    items_posted: int = 0
    items_review: int = 0
    fallback_path: str | None = None


# ---------------------------------------------------------------------------
# Utility functions on Opportunity
# ---------------------------------------------------------------------------

_TIER1 = ["gov.", ".gov", "ungm.org", "etimad.sa", "tejari.com", "gebiz.gov.sg",
           "ted.europa.eu", "worldbank.org", "undp.org", "sam.gov",
           "contracts.gov.uk", "tenders.nsw.gov.au"]
_TIER2 = ["linkedin.com", "eventbrite.com", "10times.com", "procurementleaders.com",
           "safetyandhealthmagazine.com", "ohsonline.com", "ishn.com", "ehstoday.com",
           "events.com", "conference-service.com"]


def classify_tier(url: str) -> int:
    url_lower = url.lower()
    if any(p in url_lower for p in _TIER1):
        return 1
    if any(p in url_lower for p in _TIER2):
        return 2
    return 3


def normalise_url(url: str) -> str:
    try:
        p = urlparse(url)
        q = parse_qs(p.query, keep_blank_values=False)
        clean_q = {k: v for k, v in q.items()
                   if not k.lower().startswith(("utm_", "ref", "fbclid", "gclid", "source"))}
        return urlunparse((p.scheme.lower(), p.netloc.lower(), p.path.rstrip("/"),
                           p.params, urlencode(clean_q, doseq=True), ""))
    except Exception:
        return url.lower().rstrip("/")


def compute_fingerprint(url: str, title: str, deadline: datetime | None) -> str:
    norm_url = normalise_url(url)
    norm_title = re.sub(r"\s+", " ", unicodedata.normalize("NFKD", title.lower()).strip())
    deadline_str = deadline.date().isoformat() if deadline else "none"
    raw = f"{norm_url}|{norm_title}|{deadline_str}"
    return hashlib.sha256(raw.encode()).hexdigest()


def extract_domain(url: str) -> str:
    try:
        return urlparse(url).netloc.lower().removeprefix("www.")
    except Exception:
        return ""


def strip_code_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    return text.strip()


def parse_datetime(value) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
