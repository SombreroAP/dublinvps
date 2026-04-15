"""Edge calculation + paper-trade signal logger.

IMPORTANT: bids/asks on the Market object from the discovery refresh can be
up to GAMMA_TTL seconds stale. Before logging a signal we re-fetch the CLOB
book live, so the logged ask is what a real order would actually hit.
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

import httpx
import orjson

from src.config import settings
from src.logging_setup import log
from src.polymarket.gamma import Market, fetch_clob_top

PAPER_LOG = Path("paper_trades.jsonl")


def fair_yes_probability(current: float, opening: float, seconds_left: float) -> float:
    """Given a Chainlink move and time-to-settlement, how confident are we?"""
    if seconds_left <= 0:
        return 1.0 if current >= opening else 0.0
    move_bps = (current - opening) / opening * 10_000
    if seconds_left < 10 and abs(move_bps) > 1:
        return 0.99 if move_bps > 0 else 0.01
    if seconds_left < 30 and abs(move_bps) > 3:
        return 0.95 if move_bps > 0 else 0.05
    if seconds_left < 45 and abs(move_bps) > 5:
        return 0.85 if move_bps > 0 else 0.15
    return 0.5 + (0.05 if move_bps > 0 else -0.05)


async def evaluate_and_log(
    market: Market,
    current_price: float,
    opening_price: float,
    http: httpx.AsyncClient,
    last_sig: dict,
) -> None:
    """Evaluate a single market. If it passes the edge gate, re-fetch a LIVE
    CLOB book for the relevant side, then log with fresh prices."""
    sec_left = market.seconds_remaining
    if not (settings.entry_window_end_sec < sec_left <= settings.entry_window_start_sec):
        return
    p_yes = fair_yes_probability(current_price, opening_price, sec_left)

    # Use the cached ask to decide if signal-worthy; refetch live for logging.
    side = None
    if p_yes > 0.5 and market.best_ask_yes is not None:
        side = "YES"
        cached_ask = market.best_ask_yes
        target_p = p_yes
        token_id = market.yes_token_id
    elif p_yes < 0.5 and market.best_ask_no is not None:
        side = "NO"
        cached_ask = market.best_ask_no
        target_p = 1 - p_yes
        token_id = market.no_token_id
    if side is None:
        return

    # Cheap pre-filter: if cached ask is already way above fair, don't even
    # bother with the live refetch. Leave a small tolerance since fresh price
    # could be slightly cheaper.
    cached_edge = target_p - cached_ask - market.taker_fee_at(cached_ask)
    if cached_edge < settings.edge_threshold - 0.03:
        return

    # Live refetch for authoritative ask
    refetch_start = time.monotonic()
    live_bid, live_ask = await fetch_clob_top(http, token_id)
    price_age_ms = int((time.monotonic() - refetch_start) * 1000)
    if live_ask is None:
        return

    fee = market.taker_fee_at(live_ask)
    edge = target_p - live_ask - fee
    if edge <= settings.edge_threshold:
        return

    key = (market.slug, side)
    fingerprint = (round(live_ask, 4), round(target_p, 3))
    if last_sig.get(key) == fingerprint:
        return
    last_sig[key] = fingerprint

    sig = {
        "ts": time.time(), "slug": market.slug, "asset": market.asset,
        "side": side, "ask": live_ask, "bid": live_bid,
        "cached_ask_at_discovery": cached_ask,
        "price_age_ms": price_age_ms,  # how long the live refetch took
        "fair_p": target_p, "edge": edge, "fee": fee,
        "current": current_price, "opening": opening_price, "sec_left": sec_left,
        "size_usdc": min(settings.max_position_usdc,
                         settings.max_position_usdc * edge / 0.05),
    }
    PAPER_LOG.open("ab").write(orjson.dumps(sig) + b"\n")
    log.info("paper.signal", **sig)


async def run_loop(feed, markets_provider) -> None:
    last_status = 0.0
    last_sig: dict = {}
    async with httpx.AsyncClient() as http:
        while True:
            try:
                markets, openings = await markets_provider()
                now = time.time()
                live_slugs = {m.slug for m in markets}
                for k in list(last_sig):
                    if k[0] not in live_slugs:
                        last_sig.pop(k, None)

                # Evaluate all markets concurrently — lets live refetches run
                # in parallel so we don't serialize 9+ HTTP calls.
                tasks = []
                for m in markets:
                    cur = feed.last_price.get(m.asset)
                    opn = openings.get(m.slug)
                    if cur is None or opn is None:
                        continue
                    tasks.append(evaluate_and_log(m, cur, opn, http, last_sig))
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)

                if now - last_status > 30:
                    in_window = sum(
                        1 for m in markets
                        if settings.entry_window_end_sec < m.seconds_remaining
                           <= settings.entry_window_start_sec
                    )
                    log.info("loop.heartbeat", markets=len(markets),
                             in_window=in_window,
                             prices={a: feed.last_price.get(a)
                                     for a in ("BTC", "ETH", "SOL")})
                    last_status = now
            except Exception as e:
                log.error("loop.error", error=str(e))
            await asyncio.sleep(0.5)
