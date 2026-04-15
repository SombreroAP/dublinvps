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

    async with httpx.AsyncClient() as http:
        async def provider() -> tuple[list[Market], dict[str, float]]:
            markets = await fetch_active_markets(http, horizon_sec=900)
            for m in markets:
                if m.slug not in openings and feed.last_price.get(m.asset) is not None:
                    # NOTE: this is a placeholder. True opening price = Chainlink
                    # Data Stream snapshot at round start. For paper mode we
                    # approximate from Binance at first sight; replace with a
                    # Chainlink subscription before live trading.
                    openings[m.slug] = feed.last_price[m.asset]
            return markets, openings

        await asyncio.gather(feed_task, run_loop(feed, clob, provider))


if __name__ == "__main__":
    asyncio.run(main())
