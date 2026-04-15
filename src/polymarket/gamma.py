"""Gamma API client.

Discovery strategy: don't scan /events (which buries short-duration markets).
Generate slugs directly — 5m rounds always start at unix-time multiples of 300 —
and fetch each by slug.

Slug pattern: <asset>-updown-5m-<unix_ts>  e.g. btc-updown-5m-1776249000
where <unix_ts> is round-START unix seconds.

Bonus: Gamma returns bestBid/bestAsk per market, so paper mode doesn't need
to hit the CLOB orderbook endpoint at all.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Literal

import httpx

from src.config import settings

Asset = Literal["BTC", "ETH", "SOL"]
Duration = Literal["5m"]

ASSETS: dict[Asset, str] = {"BTC": "btc", "ETH": "eth", "SOL": "sol"}
ROUND_LEN_SEC = 300


@dataclass(frozen=True)
class Market:
    slug: str
    asset: Asset
    duration: Duration
    end_ts: int
    condition_id: str
    yes_token_id: str  # "Up" outcome
    no_token_id: str   # "Down" outcome
    best_bid_yes: float | None
    best_ask_yes: float | None
    best_bid_no: float | None
    best_ask_no: float | None
    min_size: float
    tick_size: float
    fee_rate: float

    @property
    def seconds_remaining(self) -> float:
        return self.end_ts - time.time()


def _upcoming_round_starts(now: float, lookahead_sec: int) -> list[int]:
    """Round-start timestamps within [now - 60, now + lookahead_sec]. The -60 lets us
    catch the in-flight round if we're past its start."""
    first = (int(now - 60) // ROUND_LEN_SEC) * ROUND_LEN_SEC
    last = int(now + lookahead_sec)
    return list(range(first, last + 1, ROUND_LEN_SEC))


def _parse_event(evt: dict) -> Market | None:
    slug = evt.get("slug", "")
    parts = slug.split("-")
    if len(parts) != 4 or parts[1] != "updown" or parts[2] != "5m":
        return None
    asset_short = parts[0]
    asset = next((a for a, s in ASSETS.items() if s == asset_short), None)
    if asset is None:
        return None
    markets = evt.get("markets") or []
    if not markets:
        return None
    m = markets[0]
    if not m.get("acceptingOrders"):
        return None
    try:
        end_ts = int(parts[3])  # the slug timestamp IS round start; end = +300
        end_ts += ROUND_LEN_SEC
    except ValueError:
        return None
    try:
        token_ids = json.loads(m.get("clobTokenIds") or "[]")
    except (json.JSONDecodeError, TypeError):
        return None
    if len(token_ids) != 2:
        return None
    fee = (m.get("feeSchedule") or {}).get("rate", 0.0)
    # NOTE: bestBid/bestAsk in Gamma are the YES (Up) side. NO side prices
    # we approximate via complement (1 - yes_ask = no_bid implied) — for paper
    # mode that's fine; for live we should pull both books from CLOB.
    yes_bid = m.get("bestBid")
    yes_ask = m.get("bestAsk")
    no_bid = (1.0 - yes_ask) if yes_ask is not None else None
    no_ask = (1.0 - yes_bid) if yes_bid is not None else None
    return Market(
        slug=slug, asset=asset, duration="5m", end_ts=end_ts,
        condition_id=m.get("conditionId", ""),
        yes_token_id=str(token_ids[0]), no_token_id=str(token_ids[1]),
        best_bid_yes=yes_bid, best_ask_yes=yes_ask,
        best_bid_no=no_bid, best_ask_no=no_ask,
        min_size=float(m.get("orderMinSize", 5)),
        tick_size=float(m.get("orderPriceMinTickSize", 0.01)),
        fee_rate=float(fee),
    )


async def fetch_active_markets(
    client: httpx.AsyncClient,
    horizon_sec: int = 600,
) -> list[Market]:
    """Generate slugs for upcoming rounds, fetch in parallel."""
    now = time.time()
    starts = _upcoming_round_starts(now, horizon_sec)
    slugs = [f"{ASSETS[a]}-updown-5m-{ts}" for a in ASSETS for ts in starts]

    async def fetch_one(slug: str) -> Market | None:
        try:
            r = await client.get(
                f"{settings.poly_gamma_host}/events",
                params={"slug": slug}, timeout=3.0,
            )
            r.raise_for_status()
            data = r.json()
            if not data:
                return None
            return _parse_event(data[0])
        except (httpx.HTTPError, ValueError):
            return None

    import asyncio
    results = await asyncio.gather(*(fetch_one(s) for s in slugs))
    return [m for m in results if m and m.seconds_remaining > 0]
