"""Entry point. `python -m src.main` (paper mode by default)."""
from __future__ import annotations

import asyncio

import httpx

from src.config import settings
from src.feeds.binance import BinanceFeed
from src.logging_setup import configure, log
from src.polymarket.clob import PolyCLOB
from src.polymarket.gamma import Market, fetch_active_markets
from src.strategy.sniper import run_loop


async def main() -> None:
    configure()
    log.info("startup", mode=settings.mode, edge_threshold=settings.edge_threshold)

    feed = BinanceFeed()
    clob = PolyCLOB()
    feed_task = asyncio.create_task(feed.run())

    # Cache opening prices: snapped from Binance the moment we first see the market.
    openings: dict[str, float] = {}
    # Cache markets list — refresh from Gamma every 10s, not every loop tick.
    cache: dict = {"markets": [], "fetched_at": 0.0}
    GAMMA_TTL = 10.0

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
                if m.slug not in openings and feed.last_price.get(m.asset) is not None:
                    # NOTE: placeholder. True opening = Chainlink Data Stream
                    # snapshot at round start. Paper mode approximates from
                    # Binance at first sight; swap to Chainlink before live.
                    openings[m.slug] = feed.last_price[m.asset]
            return cache["markets"], openings

        await asyncio.gather(feed_task, run_loop(feed, clob, provider))


if __name__ == "__main__":
    asyncio.run(main())
