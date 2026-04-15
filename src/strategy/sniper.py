"""Edge calculation + paper-trade signal logger.

Strategy: in the entry window (T-45s to T-5s), compare Binance mid to the
round's open price. If outcome is highly probable but the favored side trades
at a discount > edge_threshold + fees, emit a signal.

Phase 1 (paper): just log. Phase 2 (live): hand off to executor.
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

import orjson

from src.config import settings
from src.feeds.binance import BinanceFeed
from src.logging_setup import log
from src.polymarket.clob import PolyCLOB
from src.polymarket.gamma import Market

PAPER_LOG = Path("paper_trades.jsonl")

# Crypto taker fee (nominal — verify live; dynamic curve peaks at p=0.50).
TAKER_FEE = 0.018


def fair_yes_probability(current: float, opening: float, seconds_left: float) -> float:
    """Crude fair-value model: deterministic if move is large vs noise.

    For 5m markets near expiry, if |current - opening| is multiple bps,
    YES (=Up) is essentially certain when current > opening. This will be
    refined with a volatility model later — but for sniping the last 30s,
    coarse is fine: the signal isn't direction, it's that Polymarket is slow.
    """
    if seconds_left <= 0:
        return 1.0 if current >= opening else 0.0
    move_bps = (current - opening) / opening * 10_000
    # Sigmoid-ish: 5 bps move with 30s left -> ~0.95 confidence
    # Tune with backtest data.
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
    yes_ask: float | None,
    no_ask: float | None,
) -> dict | None:
    """Return signal dict if edge > threshold, else None."""
    sec_left = market.seconds_remaining
    if not (settings.entry_window_end_sec < sec_left <= settings.entry_window_start_sec):
        return None

    p_yes = fair_yes_probability(current_price, opening_price, sec_left)

    # Edge after fees on the favored side
    if p_yes > 0.5 and yes_ask is not None:
        edge = p_yes - yes_ask - TAKER_FEE
        if edge > settings.edge_threshold:
            return _signal(market, "YES", yes_ask, p_yes, edge, current_price, opening_price, sec_left)
    if p_yes < 0.5 and no_ask is not None:
        p_no = 1 - p_yes
        edge = p_no - no_ask - TAKER_FEE
        if edge > settings.edge_threshold:
            return _signal(market, "NO", no_ask, p_no, edge, current_price, opening_price, sec_left)
    return None


def _signal(market: Market, side: str, ask: float, p: float, edge: float,
            cur: float, opn: float, sec_left: float) -> dict:
    return {
        "ts": time.time(), "slug": market.slug, "asset": market.asset,
        "duration": market.duration, "side": side, "ask": ask, "fair_p": p,
        "edge": edge, "current": cur, "opening": opn, "sec_left": sec_left,
        "size_usdc": min(settings.max_position_usdc, settings.max_position_usdc * edge / 0.05),
    }


def log_paper(signal: dict) -> None:
    PAPER_LOG.open("ab").write(orjson.dumps(signal) + b"\n")
    log.info("paper.signal", **signal)


async def run_loop(feed: BinanceFeed, clob: PolyCLOB, markets_provider) -> None:
    """`markets_provider` is async callable returning current Market list + opening prices."""
    while True:
        try:
            markets, openings = await markets_provider()
            for m in markets:
                cur = feed.last_price.get(m.asset)
                opn = openings.get(m.slug)
                if cur is None or opn is None:
                    continue
                yes_book = clob.top_of_book(m.yes_token_id)
                no_book = clob.top_of_book(m.no_token_id)
                sig = evaluate(m, cur, opn, yes_book.ask, no_book.ask)
                if sig:
                    log_paper(sig)
        except Exception as e:
            log.error("loop.error", error=str(e))
        await asyncio.sleep(0.5)
