"""Edge calculation + paper-trade signal logger.

Strategy: in entry window (T-45s to T-5s), compare Binance mid to round open
price. If Polymarket's YES (Up) ask is far enough below fair-value probability
that we beat fees + buffer, log a signal.

Paper mode uses bestBid/bestAsk straight from Gamma (no CLOB call).
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

import orjson

from src.config import settings
from src.feeds.binance import BinanceFeed
from src.logging_setup import log
from src.polymarket.gamma import Market

PAPER_LOG = Path("paper_trades.jsonl")


def fair_yes_probability(current: float, opening: float, seconds_left: float) -> float:
    """Coarse model: given a move size and time-to-settlement, how confident
    are we in Up vs Down? Will be replaced by a proper volatility model with
    backtest data, but for last-30s sniping the signal is dominated by big
    moves vs short windows.
    """
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


def evaluate(
    market: Market,
    current_price: float,
    opening_price: float,
) -> dict | None:
    """Edge units: fraction of $1-per-share notional.
    EV per share at price p, on a contract that pays $1 with probability fair_p:
        EV = fair_p - p - fee(p)
    where fee(p) is in fraction-of-notional. Threshold is in same units.
    """
    sec_left = market.seconds_remaining
    if not (settings.entry_window_end_sec < sec_left <= settings.entry_window_start_sec):
        return None
    p_yes = fair_yes_probability(current_price, opening_price, sec_left)

    if p_yes > 0.5 and market.best_ask_yes is not None:
        ask = market.best_ask_yes
        edge = p_yes - ask - market.taker_fee_at(ask)
        if edge > settings.edge_threshold:
            return _signal(market, "YES", ask, p_yes, edge,
                           current_price, opening_price, sec_left)
    if p_yes < 0.5 and market.best_ask_no is not None:
        p_no = 1 - p_yes
        ask = market.best_ask_no
        edge = p_no - ask - market.taker_fee_at(ask)
        if edge > settings.edge_threshold:
            return _signal(market, "NO", ask, p_no, edge,
                           current_price, opening_price, sec_left)
    return None


def _signal(market: Market, side: str, ask: float, p: float, edge: float,
            cur: float, opn: float, sec_left: float) -> dict:
    return {
        "ts": time.time(), "slug": market.slug, "asset": market.asset,
        "side": side, "ask": ask, "fair_p": p, "edge": edge,
        "current": cur, "opening": opn, "sec_left": sec_left,
        "fee": market.taker_fee_at(ask),
        "size_usdc": min(settings.max_position_usdc, settings.max_position_usdc * edge / 0.05),
    }


def log_paper(signal: dict) -> None:
    PAPER_LOG.open("ab").write(orjson.dumps(signal) + b"\n")
    log.info("paper.signal", **signal)


async def run_loop(feed: BinanceFeed, markets_provider) -> None:
    last_status = 0.0
    # Dedup: only re-log a (slug, side) signal when ask price or fair_p changes.
    last_sig: dict[tuple[str, str], tuple[float, float]] = {}
    while True:
        try:
            markets, openings = await markets_provider()
            now = time.time()
            # Garbage-collect dedup state for finished markets.
            live_slugs = {m.slug for m in markets}
            for k in list(last_sig):
                if k[0] not in live_slugs:
                    last_sig.pop(k, None)
            for m in markets:
                cur = feed.last_price.get(m.asset)
                opn = openings.get(m.slug)
                if cur is None or opn is None:
                    continue
                sig = evaluate(m, cur, opn)
                if not sig:
                    continue
                key = (sig["slug"], sig["side"])
                fingerprint = (round(sig["ask"], 4), round(sig["fair_p"], 3))
                if last_sig.get(key) == fingerprint:
                    continue
                last_sig[key] = fingerprint
                log_paper(sig)
            # Heartbeat every 30s so we know the loop is alive even if no signals.
            if now - last_status > 30:
                in_window = sum(
                    1 for m in markets
                    if settings.entry_window_end_sec < m.seconds_remaining <= settings.entry_window_start_sec
                )
                log.info("loop.heartbeat", markets=len(markets), in_window=in_window,
                         prices={a: feed.last_price.get(a) for a in ("BTC", "ETH", "SOL")})
                last_status = now
        except Exception as e:
            log.error("loop.error", error=str(e))
        await asyncio.sleep(0.5)
