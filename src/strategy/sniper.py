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
from src.polymarket.gamma import Market, fetch_clob_book, sweep_fill_ask

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


def compute_z(asset: str, current: float, opening: float,
              seconds_left: float) -> tuple[float, float, float]:
    """Return (z_score, move_bps, sd_remaining_bps). z = move / σ·√(T-s).
    z is in units of "how many standard deviations the move so far is vs the
    expected volatility in the remaining time." Large |z| = move dominates
    noise budget = likely to stick. Small |z| = move is within noise = fragile.
    """
    if opening <= 0 or seconds_left <= 0:
        return 0.0, 0.0, 0.0
    move_bps = (current - opening) / opening * 10_000
    sigma = _sigma_bps(asset)
    sd_remaining_bps = sigma * sqrt(seconds_left)
    if sd_remaining_bps < 1e-9:
        return (float("inf") if move_bps > 0 else float("-inf") if move_bps < 0 else 0.0,
                move_bps, sd_remaining_bps)
    return move_bps / sd_remaining_bps, move_bps, sd_remaining_bps


def fair_yes_probability(asset: str, current: float, opening: float,
                         seconds_left: float) -> float:
    """P(close ≥ open | current move so far, time remaining) under a zero-drift
    Brownian model for log price. For a martingale, the probability that a
    random walk ends above where it started, given current deviation x and
    time-to-go (T-s), is Φ(x / (σ·√(T-s))).
    """
    if seconds_left <= 0:
        return 1.0 if current >= opening else 0.0
    z, _, sd = compute_z(asset, current, opening, seconds_left)
    if sd < 1e-9:
        return 1.0 if current >= opening else 0.0
    return _phi(z)


async def evaluate_and_log(
    market: Market,
    current_price: float,
    opening_price: float,
    http: httpx.AsyncClient,
    last_sig: dict,
    binance_price: float | None = None,
) -> None:
    """Evaluate a single market. If it passes the edge gate, re-fetch a LIVE
    CLOB book for the relevant side, then log with fresh prices."""
    sec_left = market.seconds_remaining
    if not (settings.entry_window_end_sec < sec_left <= settings.entry_window_start_sec):
        return

    # z-score gate: the move so far must be statistically significant vs the
    # expected remaining volatility. Small moves with lots of time left are
    # fragile — they reverse. Data showed 4/4 historical losses had |z|<1.5.
    z, move_bps, sd_rem = compute_z(market.asset, current_price,
                                     opening_price, sec_left)
    if abs(z) < settings.min_z_score:
        return

    p_yes = fair_yes_probability(market.asset, current_price, opening_price, sec_left)

    # Use the cached ask to decide if signal-worthy; refetch live for logging.
    allowed = {s.strip() for s in settings.enabled_sides.split(",")}
    side = None
    if p_yes > 0.5 and market.best_ask_yes is not None and "YES" in allowed:
        side = "YES"
        cached_ask = market.best_ask_yes
        target_p = p_yes
        token_id = market.yes_token_id
    elif p_yes < 0.5 and market.best_ask_no is not None and "NO" in allowed:
        side = "NO"
        cached_ask = market.best_ask_no
        target_p = 1 - p_yes
        token_id = market.no_token_id
    if side is None:
        return

    # Require high model confidence. Data showed fair_p<0.85 wins ~25%.
    if target_p < settings.min_fair_p:
        return

    # Cross-feed sanity check. If our Chainlink "current" diverges from
    # Binance mid by too much, Chainlink is probably stale (this caused
    # our SOL loss — Chainlink lagged a real crash by ~15s). Skip.
    feed_div_bps = None
    if binance_price is not None and settings.max_feed_divergence_bps > 0:
        feed_div_bps = (current_price - binance_price) / binance_price * 10_000
        if abs(feed_div_bps) > settings.max_feed_divergence_bps:
            return

    # Cheap pre-filter: if cached ask is already way above fair, don't even
    # bother with the live refetch. Leave a small tolerance since fresh price
    # could be slightly cheaper.
    cached_edge = target_p - cached_ask - market.taker_fee_at(cached_ask)
    if cached_edge < settings.edge_threshold - 0.03:
        return

    # Live book refetch with DEPTH. Compute:
    #   fill_ask    — VWAP price we'd actually pay for our desired size
    #   market_mid  — (best_bid + best_ask)/2 of the side we're buying; this
    #                 is the market's own implied probability that our side wins.
    # If our fair_p disagrees with the market by too much, SKIP — the market
    # likely has information (e.g. Binance/Chainlink divergence) we don't.
    refetch_start = time.monotonic()
    desired_size = settings.max_position_usdc
    bids, asks = await fetch_clob_book(http, token_id)
    price_age_ms = int((time.monotonic() - refetch_start) * 1000)
    if not asks:
        return
    best_ask = asks[0][0]
    best_bid = bids[0][0] if bids else None
    fill_ask, fillable = sweep_fill_ask(asks, desired_size)
    if fill_ask is None or fillable < 5.0:  # dust book
        return
    # Reality filters — prevent logging signals that couldn't be filled:
    #   1. Ask below $0.05: typically a handful of dust shares; real queue
    #      behind them means we'd never hit this price at meaningful size.
    #   2. Claimed edge > 30%: if the market is ACTUALLY pricing 30pp below
    #      our fair_p, we are wrong, not the market. Likely stale data.
    #   3. Require at least 80% of desired size fillable.
    if fill_ask < 0.05:
        return
    if fillable < 0.8 * desired_size:
        return
    # Market's implied probability for OUR side. Use mid if we have both,
    # else fall back to best_ask.
    market_implied = ((best_bid + best_ask) / 2) if (best_bid is not None) else best_ask
    disagreement = abs(target_p - market_implied)
    if disagreement > settings.max_disagreement:
        return

    fee = market.taker_fee_at(fill_ask)
    edge = target_p - fill_ask - fee
    if edge <= settings.edge_threshold:
        return
    # Hard cap: edge > 30% almost certainly means stale data or a model bug,
    # not real alpha. Markets don't leave 30pp on the table for seconds.
    if edge > 0.30:
        return

    key = (market.slug, side)
    # Tighter dedup: fingerprint on fill_ask rounded to 2dp + fair_p to 2dp.
    # Small orderbook wobbles no longer trigger new log lines.
    fingerprint = (round(fill_ask, 2), round(target_p, 2))
    if last_sig.get(key) == fingerprint:
        return
    last_sig[key] = fingerprint

    # Half-Kelly sizing.
    # For binary contract bought at price p with fair win-prob q (and fee
    # already subtracted from edge), the Kelly-optimal fraction of bankroll
    # is f* = edge / (1 - p). Half-Kelly = 0.5 * f* gives ~99% of geometric
    # growth at half the variance.
    kelly_full = edge / (1 - fill_ask) if fill_ask < 1 else 0.0
    kelly_size = settings.bankroll_usdc * kelly_full * settings.kelly_fraction
    size_usdc = max(market.min_size,
                    min(fillable, settings.max_position_usdc, kelly_size))

    sig = {
        "ts": time.time(), "slug": market.slug, "asset": market.asset,
        "side": side,
        "ask": fill_ask,                       # VWAP we'd actually pay
        "best_ask": best_ask,                  # top-of-book reference
        "best_bid": best_bid,
        "market_implied_p": market_implied,    # (bid+ask)/2 of our side
        "disagreement": disagreement,
        "cached_ask_at_discovery": cached_ask,
        "fillable_usdc": fillable,
        "price_age_ms": price_age_ms,
        "fair_p": target_p, "edge": edge, "fee": fee,
        "z_score": z, "move_bps": move_bps, "sd_remaining_bps": sd_rem,
        "current": current_price, "opening": opening_price, "sec_left": sec_left,
        "binance_price": binance_price,
        "feed_divergence_bps": feed_div_bps,
        "kelly_full_frac": kelly_full,
        "kelly_size_uncapped": kelly_size,
        "size_usdc": size_usdc,
    }
    PAPER_LOG.open("ab").write(orjson.dumps(sig) + b"\n")
    log.info("paper.signal", **sig)


async def run_loop(feed, markets_provider, binance=None) -> None:
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
                    bn = binance.last_price.get(m.asset) if binance is not None else None
                    tasks.append(evaluate_and_log(m, cur, opn, http, last_sig, bn))
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
