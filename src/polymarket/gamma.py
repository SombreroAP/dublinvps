"""Gamma API client — discover active 5m/15m crypto Up/Down markets.

5m slug pattern: btc-updown-5m-<unix_ts>, eth-updown-5m-<ts>, sol-updown-5m-<ts>
  where <ts> is round-START unix seconds (multiple of 300).
15m/hourly slug: bitcoin-up-or-down-<month>-<day>-<year>-<hh><am|pm>-et
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Literal

import httpx

from src.config import settings

Asset = Literal["BTC", "ETH", "SOL"]
Duration = Literal["5m", "15m", "1h"]

SLUG_5M = re.compile(r"^(btc|eth|sol)-updown-5m-(\d+)$")
SLUG_LONG = re.compile(r"^(bitcoin|ethereum|solana)-up-or-down-")

_ASSET_MAP = {"btc": "BTC", "eth": "ETH", "sol": "SOL",
              "bitcoin": "BTC", "ethereum": "ETH", "solana": "SOL"}


@dataclass(frozen=True)
class Market:
    slug: str
    asset: Asset
    duration: Duration
    end_ts: int           # unix seconds
    condition_id: str
    yes_token_id: str
    no_token_id: str

    @property
    def seconds_remaining(self) -> float:
        return self.end_ts - time.time()


def _classify(slug: str, end_ts: int) -> tuple[Asset, Duration] | None:
    if m := SLUG_5M.match(slug):
        return _ASSET_MAP[m.group(1)], "5m"  # type: ignore[return-value]
    if m := SLUG_LONG.match(slug):
        # Heuristic: distinguish 15m vs 1h by round-end-minute. Both share the long slug
        # form on Polymarket; refine once we see live data.
        asset = _ASSET_MAP[m.group(1)]
        return asset, "1h"  # type: ignore[return-value]
    return None


async def fetch_active_markets(
    client: httpx.AsyncClient,
    horizon_sec: int = 3600,
) -> list[Market]:
    """Pull active crypto Up/Down markets ending within `horizon_sec`."""
    now = int(time.time())
    params = {"closed": "false", "active": "true", "limit": 200}
    r = await client.get(f"{settings.poly_gamma_host}/events", params=params, timeout=5.0)
    r.raise_for_status()
    out: list[Market] = []
    for evt in r.json():
        slug = evt.get("slug", "")
        kind = _classify(slug, 0)
        if not kind:
            continue
        # Each event contains markets[]; for binary up/down there's one market
        # with a `tokens` list of YES/NO outcomes.
        for mkt in evt.get("markets", []):
            end_iso = mkt.get("endDate") or evt.get("endDate")
            if not end_iso:
                continue
            try:
                end_ts = int(httpx._utils.parse_iso8601(end_iso).timestamp())  # type: ignore[attr-defined]
            except Exception:
                # Fallback: tolerate ISO strings with Z
                from datetime import datetime
                end_ts = int(datetime.fromisoformat(end_iso.replace("Z", "+00:00")).timestamp())
            if end_ts - now > horizon_sec or end_ts <= now:
                continue
            tokens = mkt.get("tokens") or []
            yes_id = next((t["token_id"] for t in tokens if t.get("outcome", "").lower() == "yes"), None)
            no_id = next((t["token_id"] for t in tokens if t.get("outcome", "").lower() == "no"), None)
            if not yes_id or not no_id:
                continue
            asset, duration = kind
            out.append(Market(
                slug=slug, asset=asset, duration=duration, end_ts=end_ts,
                condition_id=mkt.get("conditionId") or mkt.get("condition_id", ""),
                yes_token_id=yes_id, no_token_id=no_id,
            ))
    return out
