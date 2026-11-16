"""History database — single table for deduplication across runs."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

import aiosqlite

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS history (
    fingerprint     TEXT PRIMARY KEY,
    url             TEXT NOT NULL,
    title           TEXT NOT NULL,
    category        TEXT NOT NULL DEFAULT '',
    geography       TEXT NOT NULL DEFAULT '',
    organizer       TEXT,
    deadline        TEXT,
    score           INTEGER NOT NULL DEFAULT 0,
    first_seen      TEXT NOT NULL,
    last_seen       TEXT NOT NULL,
    posted          INTEGER NOT NULL DEFAULT 0,
    posted_at       TEXT,
    language        TEXT NOT NULL DEFAULT 'en',
    summary         TEXT NOT NULL DEFAULT ''
);
"""


async def init(conn: aiosqlite.Connection) -> None:
    await conn.executescript(_SCHEMA)
    await conn.commit()


async def exists(conn: aiosqlite.Connection, fingerprint: str) -> bool:
    async with conn.execute(
        "SELECT 1 FROM history WHERE fingerprint = ?", (fingerprint,)
    ) as cur:
        return await cur.fetchone() is not None


async def upsert(conn: aiosqlite.Connection, fp: str, url: str, title: str,
                 category: str, geography: str, organizer: str | None,
                 deadline: datetime | None, score: int, posted_at: datetime | None,
                 language: str, summary: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    await conn.execute("""
        INSERT INTO history (fingerprint, url, title, category, geography, organizer,
                             deadline, score, first_seen, last_seen, posted, posted_at,
                             language, summary)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(fingerprint) DO UPDATE SET
            last_seen = excluded.last_seen,
            posted = excluded.posted,
            posted_at = excluded.posted_at,
            score = excluded.score,
            summary = excluded.summary
    """, (fp, url, title, category, geography, organizer,
          deadline.isoformat() if deadline else None,
          score, now, now,
          1 if posted_at else 0,
          posted_at.isoformat() if posted_at else None,
          language, summary))
    await conn.commit()


async def prune(conn: aiosqlite.Connection, retention_days: int) -> int:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
    async with conn.execute(
        "DELETE FROM history WHERE last_seen < ?", (cutoff,)
    ) as cur:
        count = cur.rowcount
    await conn.commit()
    logger.info("Pruned %d old history records", count)
    return count
