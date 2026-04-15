"""Edge calculation + paper-trade signal logger.

IMPORTANT: bids/asks on the Market object from the discovery refresh can be
up to GAMMA_TTL seconds stale. Before logging a signal we re-fetch the CLOB
book live, so the logged ask is what a real order would actually hit.
"""
from __future__ import annotations

import asyncio
import time
from math import erf, sqrt
from pathlib import Path

import httpx
import orjson

from src.config import settings
from src.logging_setup import log
from src.polymarket.gamma import Market, fetch_clob_fill_ask

PAPER_LOG = Path("paper_trades.jsonl")


def _sigma_bps(asset: str) -> float:
    return {
        "BTC": settings.sigma_bps_btc,
        "ETH": settings.sigma_bps_eth,
        "SOL": settings.sigma_bps_sol,
    }.get(asset, 1.2)


def _phi(z: float) -> float:
    """Standard normal CDF."""
    return 0.5 * (1.0 + erf(z / sqrt(2)))


def fair_yes_probability(asset: str, current: float, opening: float,
                         seconds_left: float) -> float:
    """P(close ≥ open | current move so far, time remaining) under a zero-drift
    Brownian model for log price. For a martingale, the probability that a
    random walk ends above where it started, given current deviation x and
    time-to-go (T-s), is Φ(x / (σ·√(T-s))).

    move_bps: current deviation in bps
    σ: per-√second volatility in bps (asset-dependent, configured).
    Ties resolve UP on Polymarket → x ≥ 0 is UP.
    """
    if seconds_left <= 0:
        return 1.0 if current >= opening else 0.0
    move_bps = (current - opening) / opening * 10_000
    sigma = _sigma_bps(asset)
    sd_remaining_bps = sigma * sqrt(seconds_left)
    if sd_remaining_bps < 1e-9:
        return 1.0 if move_bps >= 0 else 0.0
    z = move_bps / sd_remaining_bps
    return _phi(z)


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
    p_yes = fair_yes_probability(market.asset, current_price, opening_price, sec_left)

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

    # Live refetch with DEPTH — compute the VWAP ask we'd actually pay for
    # our position size, not the dust-order best ask. This kills the
    # "$25 at 0.001 ask" fantasy fills.
    refetch_start = time.monotonic()
    desired_size = settings.max_position_usdc
    fill_ask, best_ask, fillable = await fetch_clob_fill_ask(
        http, token_id, desired_size,
    )
    price_age_ms = int((time.monotonic() - refetch_start) * 1000)
    if fill_ask is None:
        return
    # If the book can't absorb at least $5 of our order, skip — dust.
    if fillable < 5.0:
        return

    fee = market.taker_fee_at(fill_ask)
    edge = target_p - fill_ask - fee
    if edge <= settings.edge_threshold:
        return

    key = (market.slug, side)
    fingerprint = (round(fill_ask, 4), round(target_p, 3))
    if last_sig.get(key) == fingerprint:
        return
    last_sig[key] = fingerprint

    sig = {
        "ts": time.time(), "slug": market.slug, "asset": market.asset,
        "side": side,
        "ask": fill_ask,                       # VWAP we'd actually pay
        "best_ask": best_ask,                  # top-of-book reference
        "cached_ask_at_discovery": cached_ask,
        "fillable_usdc": fillable,
        "price_age_ms": price_age_ms,
        "fair_p": target_p, "edge": edge, "fee": fee,
        "current": current_price, "opening": opening_price, "sec_left": sec_left,
        "size_usdc": min(fillable, settings.max_position_usdc,
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
