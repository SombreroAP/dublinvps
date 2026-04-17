"""Entry point. `python -m src.main` (paper mode by default).

Architecture:
- ChainlinkFeed: Polymarket RTDS WebSocket. SAME signed price the resolver
  uses → opening + current = Chainlink (zero basis risk at boundaries).
- BinanceFeed: secondary, sub-second momentum signal (kept for future use).
- Gamma poller: enumerate upcoming 5m markets every 10s.
- Sniper loop: evaluate edge, dedup, log paper signals.
"""
from __future__ import annotations

import asyncio

import httpx

from src.config import settings
from src.feeds.binance import BinanceFeed
from src.feeds.book_logger import run as run_book_logger
from src.feeds.chainlink import ChainlinkFeed
from src.logging_setup import configure, log
from src.polymarket.gamma import Market, fetch_active_markets
from src.strategy.sniper import run_loop


async def main() -> None:
    configure()
    log.info("startup", mode=settings.mode, edge_threshold=settings.edge_threshold)

    chainlink = ChainlinkFeed()
    binance = BinanceFeed()
    cl_task = asyncio.create_task(chainlink.run())
    bn_task = asyncio.create_task(binance.run())

    cache: dict = {"markets": [], "fetched_at": 0.0}
    GAMMA_TTL = 3.0
    openings: dict[str, float] = {}

    async with httpx.AsyncClient() as http:
        async def provider() -> tuple[list[Market], dict[str, float]]:
            now = asyncio.get_event_loop().time()
            if now - cache["fetched_at"] > GAMMA_TTL:
                try:
                    cache["markets"] = await fetch_active_markets(http, horizon_sec=900)
                    cache["fetched_at"] = now
                    log.info("gamma.refresh", count=len(cache["markets"]))
                except Exception as e:
                    log.error("gamma.error", error=str(e))
            for m in cache["markets"]:
                if m.slug in openings:
                    continue
                round_start = m.end_ts - 300  # 5m markets only
                op = chainlink.opening_at(m.asset, round_start)
                if op is not None:
                    openings[m.slug] = op
                    log.info("opening.snapped", slug=m.slug, asset=m.asset,
                             round_start=round_start, value=op)
            return cache["markets"], openings

        await asyncio.gather(
            cl_task,
            bn_task,
            run_loop(chainlink, provider, binance=binance),
            run_book_logger(provider),
        )


if __name__ == "__main__":
    asyncio.run(main())
