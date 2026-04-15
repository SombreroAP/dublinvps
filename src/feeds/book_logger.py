"""Orderbook snapshot logger.

For every market in the entry window (T-45 → T-5), snapshots both YES and NO
CLOB books every 500ms. Writes one JSONL file per UTC day:
    /opt/sniper/orderbooks/YYYY-MM-DD.jsonl

Each line:
    {"ts": <unix_float>, "slug": "...", "asset": "BTC", "side": "YES",
     "sec_left": 23.4, "bids": [[p,s], ...], "asks": [[p,s], ...]}

Usage (offline): pull a day's file and analyze book depth around signals,
entry-time fill realism, final-second mispricings, etc.

Size estimate: ~6 books per 500ms in entry windows × ~10 entry windows per
hour per asset × 3 assets ≈ 720 rows × 40s-window ÷ 500ms ≈ a few MB per day.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import orjson

from src.config import settings
from src.logging_setup import log
from src.polymarket.gamma import Market, fetch_clob_book

LOG_DIR = Path("/opt/sniper/orderbooks")
SNAPSHOT_INTERVAL_SEC = 0.5
BOOK_DEPTH = 10  # top-N levels each side


def _entry_window_markets(markets: list[Market]) -> list[Market]:
    lo, hi = settings.entry_window_end_sec, settings.entry_window_start_sec
    return [m for m in markets if lo < m.seconds_remaining <= hi]


def _daily_path() -> Path:
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    return LOG_DIR / f"{day}.jsonl"


async def _snap_market(client: httpx.AsyncClient, m: Market) -> list[dict]:
    out = []
    for side, tid in [("YES", m.yes_token_id), ("NO", m.no_token_id)]:
        bids, asks = await fetch_clob_book(client, tid)
        out.append({
            "ts": time.time(),
            "slug": m.slug,
            "asset": m.asset,
            "side": side,
            "sec_left": round(m.seconds_remaining, 2),
            "token_id": tid,
            "bids": [[p, s] for p, s in bids[:BOOK_DEPTH]],
            "asks": [[p, s] for p, s in asks[:BOOK_DEPTH]],
        })
    return out


async def run(markets_provider) -> None:
    async with httpx.AsyncClient() as http:
        while True:
            start = time.monotonic()
            try:
                markets, _ = await markets_provider()
                window = _entry_window_markets(markets)
                if window:
                    snaps_nested = await asyncio.gather(
                        *(_snap_market(http, m) for m in window),
                        return_exceptions=True,
                    )
                    rows = [row for result in snaps_nested
                            if not isinstance(result, Exception)
                            for row in result]
                    if rows:
                        path = _daily_path()
                        with path.open("ab") as f:
                            for r in rows:
                                f.write(orjson.dumps(r) + b"\n")
            except Exception as e:
                log.error("book_logger.error", error=str(e))
            elapsed = time.monotonic() - start
            await asyncio.sleep(max(0, SNAPSHOT_INTERVAL_SEC - elapsed))
