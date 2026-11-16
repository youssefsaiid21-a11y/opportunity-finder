"""Async context managers for external services: Serper, Playwright, Anthropic."""

from __future__ import annotations

import asyncio
import io
import logging
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

import httpx
import anthropic
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from config import AppConfig

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Serper search client
# ---------------------------------------------------------------------------

@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str = ""
    source: str = ""


class SerperClient:
    def __init__(self, api_key: str, concurrency: int = 5):
        self._api_key = api_key
        self._semaphore = asyncio.Semaphore(concurrency)
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self):
        self._client = httpx.AsyncClient(timeout=30.0)
        return self

    async def __aexit__(self, *exc):
        if self._client:
            await self._client.aclose()

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10),
           retry=retry_if_exception_type(httpx.HTTPError), reraise=True)
    async def _post(self, query: str, num: int) -> list[SearchResult]:
        resp = await self._client.post(
            "https://google.serper.dev/search",
            json={"q": query, "num": num},
            headers={"X-API-KEY": self._api_key, "Content-Type": "application/json"},
        )
        if resp.status_code == 429:
            raise httpx.HTTPStatusError("Rate limited", request=resp.request, response=resp)
        if resp.status_code != 200:
            logger.warning("Serper HTTP %d for '%s'", resp.status_code, query[:60])
            return []
        data = resp.json()
        return [
            SearchResult(
                title=r.get("title", ""),
                url=r.get("link", ""),
                snippet=r.get("snippet", ""),
                source=urlparse(r.get("link", "")).netloc.lower().removeprefix("www."),
            )
            for r in data.get("organic", [])
        ]

    async def search(self, query: str, num: int = 10) -> list[SearchResult]:
        async with self._semaphore:
            try:
                return await self._post(query, num)
            except Exception as exc:
                logger.warning("Search failed for '%s': %s", query[:60], exc)
                return []


# ---------------------------------------------------------------------------
# DuckDuckGo fallback search client (free, no API key)
# ---------------------------------------------------------------------------

class DuckDuckGoClient:
    """Free search fallback using duckduckgo-search. Same interface as SerperClient.

    DDG rate-limits aggressively, so we reuse a single session and enforce
    a minimum 5s gap between requests. Retries with 60s backoff on rate-limit.
    """

    # DDG allows ~12 queries before throttling, then needs ~60s cooldown
    _BATCH_SIZE = 10
    _QUERY_GAP = 8.0       # seconds between queries within a batch
    _BATCH_COOLDOWN = 65.0  # seconds between batches

    def __init__(self, concurrency: int = 1, region: str = "wt-wt"):
        self._semaphore = asyncio.Semaphore(concurrency)
        self._region = region
        self._last_request = 0.0
        self._query_count = 0  # queries since last batch cooldown
        self._ddgs = None

    async def __aenter__(self):
        from duckduckgo_search import DDGS
        self._ddgs = DDGS()
        return self

    async def __aexit__(self, *exc):
        self._ddgs = None

    async def search(self, query: str, num: int = 10) -> list[SearchResult]:
        import time
        async with self._semaphore:
            # Batch cooldown: after _BATCH_SIZE queries, pause to avoid rate-limit
            if self._query_count >= self._BATCH_SIZE:
                logger.info("DDG batch cooldown: pausing %ds after %d queries",
                            int(self._BATCH_COOLDOWN), self._query_count)
                await asyncio.sleep(self._BATCH_COOLDOWN)
                self._query_count = 0

            # Enforce minimum gap between queries
            now = time.monotonic()
            wait = max(0, self._QUERY_GAP - (now - self._last_request))
            if wait > 0:
                await asyncio.sleep(wait)

            for attempt in range(3):
                try:
                    loop = asyncio.get_running_loop()
                    results = await loop.run_in_executor(
                        None, self._search_sync, self._ddgs, query, num, self._region)
                    self._last_request = time.monotonic()
                    self._query_count += 1
                    return results
                except Exception as exc:
                    err_str = str(exc)
                    if "Ratelimit" in err_str and attempt < 2:
                        backoff = 65 * (attempt + 1)
                        logger.info("DDG rate-limited, backing off %ds (attempt %d/3)", backoff, attempt + 1)
                        await asyncio.sleep(backoff)
                        self._last_request = time.monotonic()
                        self._query_count = 0  # reset after cooldown
                        continue
                    logger.warning("DDG search failed for '%s': %s", query[:60], exc)
                    self._last_request = time.monotonic()
                    self._query_count += 1
                    return []

    @staticmethod
    def _search_sync(ddgs, query: str, num: int, region: str) -> list[SearchResult]:
        hits = ddgs.text(query, region=region, max_results=num, backend="html")
        return [
            SearchResult(
                title=h.get("title", ""),
                url=h.get("href", ""),
                snippet=h.get("body", ""),
                source=urlparse(h.get("href", "")).netloc.lower().removeprefix("www."),
            )
            for h in hits
            if h.get("href")
        ]


# ---------------------------------------------------------------------------
# Google Custom Search (Programmable Search Engine) — 100 free queries/day
# ---------------------------------------------------------------------------

class GoogleCSEClient:
    """Google Custom Search API client. Same interface as SerperClient.

    Free tier: 100 queries/day. Requires:
    - API key from https://console.cloud.google.com/apis/credentials
    - Custom Search Engine ID from https://programmablesearchengine.google.com/
      (create one with "Search the entire web" enabled)
    """

    def __init__(self, api_key: str, cse_id: str, concurrency: int = 5):
        self._api_key = api_key
        self._cse_id = cse_id
        self._semaphore = asyncio.Semaphore(concurrency)
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self):
        self._client = httpx.AsyncClient(timeout=30.0)
        return self

    async def __aexit__(self, *exc):
        if self._client:
            await self._client.aclose()

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10),
           retry=retry_if_exception_type(httpx.HTTPError), reraise=True)
    async def _get(self, query: str, num: int) -> list[SearchResult]:
        resp = await self._client.get(
            "https://www.googleapis.com/customsearch/v1",
            params={
                "key": self._api_key,
                "cx": self._cse_id,
                "q": query,
                "num": min(num, 10),  # Google CSE max is 10 per request
            },
        )
        if resp.status_code == 429:
            raise httpx.HTTPStatusError("Rate limited", request=resp.request, response=resp)
        if resp.status_code != 200:
            logger.warning("Google CSE HTTP %d for '%s'", resp.status_code, query[:60])
            return []
        data = resp.json()
        return [
            SearchResult(
                title=r.get("title", ""),
                url=r.get("link", ""),
                snippet=r.get("snippet", ""),
                source=urlparse(r.get("link", "")).netloc.lower().removeprefix("www."),
            )
            for r in data.get("items", [])
        ]

    async def search(self, query: str, num: int = 10) -> list[SearchResult]:
        async with self._semaphore:
            try:
                return await self._get(query, num)
            except Exception as exc:
                logger.warning("Google CSE failed for '%s': %s", query[:60], exc)
                return []


# ---------------------------------------------------------------------------
# Playwright browser client
# ---------------------------------------------------------------------------

@dataclass
class PageContent:
    url: str
    title: str = ""
    text: str = ""
    pdf_links: list[str] = field(default_factory=list)
    error: str | None = None


_PDF_RE = re.compile(r'href=["\']([^"\']*\.pdf[^"\']*)["\']', re.IGNORECASE)


class Browser:
    def __init__(self, concurrency: int = 10):
        self._semaphore = asyncio.Semaphore(concurrency)
        self._browser = None
        self._pw = None

    async def __aenter__(self):
        try:
            from playwright.async_api import async_playwright
            self._pw = await async_playwright().start()
            self._browser = await self._pw.chromium.launch(
                headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"]
            )
            logger.info("Playwright browser started")
        except Exception as exc:
            logger.error("Failed to start browser: %s", exc)
            self._browser = None
        return self

    async def __aexit__(self, *exc):
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()
        logger.info("Playwright browser closed")

    async def fetch(self, url: str, timeout_ms: int = 10000) -> PageContent:
        if not self._browser:
            return PageContent(url=url, error="Browser not available")

        async with self._semaphore:
            page = None
            try:
                from playwright.async_api import TimeoutError as PwTimeout
                page = await self._browser.new_page()
                await page.route("**/*.{png,jpg,jpeg,gif,ico,woff,woff2,ttf,mp4,webm}",
                                 lambda route: route.abort())
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                except PwTimeout:
                    pass  # extract whatever loaded

                title = await page.title() if page else ""
                try:
                    text = await page.inner_text("body")
                except Exception:
                    text = ""
                html = await page.content()
                pdf_links = list(dict.fromkeys(
                    urljoin(url, h) for h in _PDF_RE.findall(html)
                    if urlparse(urljoin(url, h)).scheme in ("http", "https")
                ))
                return PageContent(url=url, title=title, text=text, pdf_links=pdf_links)
            except Exception as exc:
                return PageContent(url=url, error=str(exc))
            finally:
                if page:
                    try:
                        await page.close()
                    except Exception:
                        pass

    async def extract_pdf(self, url: str) -> str:
        """Download and extract text from a PDF URL."""
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(url, follow_redirects=True)
                if resp.status_code != 200 or len(resp.content) < 100:
                    return ""
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(None, _extract_pdf_sync, resp.content)
        except Exception as exc:
            logger.warning("PDF extraction failed for %s: %s", url, exc)
            return ""


def _extract_pdf_sync(pdf_bytes: bytes) -> str:
    try:
        import pdfplumber
        buf = io.BytesIO(pdf_bytes)
        pages = []
        with pdfplumber.open(buf) as pdf:
            for i, page in enumerate(pdf.pages[:10]):
                text = page.extract_text() or ""
                if text.strip():
                    pages.append(text)
        return "\n\n".join(pages)
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Anthropic LLM client
# ---------------------------------------------------------------------------

class LLMClient:
    def __init__(self, api_key: str):
        self._client: anthropic.AsyncAnthropic | None = None
        self._api_key = api_key

    async def __aenter__(self):
        self._client = anthropic.AsyncAnthropic(api_key=self._api_key)
        return self

    async def __aexit__(self, *exc):
        self._client = None

    async def complete(self, system: str, user: str, model: str,
                       max_tokens: int = 4096, temperature: float = 0.1) -> str:
        msg = await self._client.messages.create(
            model=model, max_tokens=max_tokens, temperature=temperature,
            system=system, messages=[{"role": "user", "content": user}],
        )
        return msg.content[0].text
