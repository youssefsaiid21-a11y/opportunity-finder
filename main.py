"""Entry point for OpportunityFinder V2."""

from __future__ import annotations

import argparse
import asyncio
import io
import logging
import sys

from config import load_config
from pipeline import run


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(
            io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        )],
    )
    for name in ("httpx", "playwright", "anthropic"):
        logging.getLogger(name).setLevel(logging.WARNING)


async def main() -> int:
    parser = argparse.ArgumentParser(description="Example Company OpportunityFinder V2")
    parser.add_argument("--run-now", action="store_true", help="Run once and exit")
    parser.add_argument("--config", default="config.json", help="Config file path")
    args = parser.parse_args()

    try:
        config = load_config(args.config)
    except (FileNotFoundError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    _setup_logging(config.log_level)
    logger = logging.getLogger(__name__)
    logger.info("OpportunityFinder V2 starting (model=%s)", config.llm_model)

    if args.run_now:
        result = await run(config)
        logger.info("Done — status=%s, posted=%d", result.status, result.items_posted)
        return 0 if result.status != "failed" else 1

    # Scheduled mode: run immediately, then every N days
    logger.info("Scheduled mode: every %d day(s)", config.scrape_frequency_days)
    while True:
        result = await run(config)
        logger.info("Run complete — status=%s. Next in %d day(s).",
                     result.status, config.scrape_frequency_days)
        await asyncio.sleep(config.scrape_frequency_days * 86400)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
