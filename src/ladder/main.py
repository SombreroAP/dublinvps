"""Ladder bot entry point. `python -m src.ladder.main` (paper-only).

Distinct from the main sniper. Logs to paper_trades_ladder.jsonl.
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

import httpx
import orjson

from src.config import settings
from src.feeds.chainlink import ChainlinkFeed
from src.ladder.strategy import evaluate, signal_to_dict
from src.logging_setup import configure, log
from src.polymarket.gamma import Market, fetch_active_markets

PAPER_LOG = Path("paper_trades_ladder.jsonl")


async def main() -> None:
    configure()
    log.info("ladder.startup", strategy="ladder_fade_v1")

    chainlink = ChainlinkFeed()
    cl_task = asyncio.create_task(chainlink.run())

    cache: dict = {"markets": [], "fetched_at": 0.0}
    GAMMA_TTL = 5.0
    openings: dict[str, float] = {}
    fired: set[tuple[str, str]] = set()  # (slug, side) — at most one ladder per round-side
    last_status = 0.0

    async with httpx.AsyncClient() as http:
        async def provider() -> tuple[list[Market], dict[str, float]]:
            now = asyncio.get_event_loop().time()
            if now - cache["fetched_at"] > GAMMA_TTL:
                try:
                    cache["markets"] = await fetch_active_markets(http, horizon_sec=600)
                    cache["fetched_at"] = now
                except Exception as e:
                    log.error("ladder.gamma.error", error=str(e))
            for m in cache["markets"]:
                if m.slug in openings:
                    continue
                round_start = m.end_ts - 300
                op = chainlink.opening_at(m.asset, round_start)
                if op is not None:
                    openings[m.slug] = op
            # GC fired-set for finished rounds
            live_slugs = {m.slug for m in cache["markets"]}
            for k in list(fired):
                if k[0] not in live_slugs:
                    fired.discard(k)
            return cache["markets"], openings

        async def loop() -> None:
            nonlocal last_status
            while True:
                try:
                    markets, openings_ = await provider()
                    now = time.time()
                    for m in markets:
                        cur = chainlink.last_price.get(m.asset)
                        opn = openings_.get(m.slug)
                        if cur is None or opn is None:
                            continue
                        sig = evaluate(m, cur, opn)
                        if sig is None:
                            continue
                        key = (sig.slug, sig.side)
                        if key in fired:
                            continue
                        fired.add(key)
                        d = signal_to_dict(sig)
                        PAPER_LOG.open("ab").write(orjson.dumps(d) + b"\n")
                        log.info("ladder.signal", **d)
                    if now - last_status > 30:
                        log.info("ladder.heartbeat", markets=len(markets),
                                 openings=len(openings_), fired=len(fired))
                        last_status = now
                except Exception as e:
                    log.error("ladder.loop.error", error=str(e))
                await asyncio.sleep(1.0)

        await asyncio.gather(cl_task, loop())


if __name__ == "__main__":
    asyncio.run(main())
