"""Polymarket RTDS WebSocket — Chainlink price stream.

Subscribes to wss://ws-live-data.polymarket.com → topic `crypto_prices_chainlink`.
This is the EXACT signed price Polymarket's resolver uses for 5m/15m crypto
markets, so it eliminates the basis risk of using Binance as a proxy.

Schema (reverse-engineered, see docs/polymarket-api-research.md):
  Subscribe:  {"action":"subscribe",
               "subscriptions":[{"topic":"crypto_prices_chainlink",
                                  "type":"*","filters":""}]}
  Update:     {"topic":"crypto_prices_chainlink","type":"update",
               "timestamp":<ms>,
               "payload":{"symbol":"btc/usd","timestamp":<ms>,"value":<float>}}
"""
from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from typing import Final

import orjson
import websockets
from tenacity import retry, stop_never, wait_exponential

from src.logging_setup import log

URL: Final = "wss://ws-live-data.polymarket.com"
SYMBOL_TO_ASSET: Final = {"btc/usd": "BTC", "eth/usd": "ETH", "sol/usd": "SOL"}
SUBSCRIBE: Final = {
    "action": "subscribe",
    "subscriptions": [
        {"topic": "crypto_prices_chainlink", "type": "*", "filters": ""}
    ],
}


class ChainlinkFeed:
    """Streams Chainlink prices and serves opening-price queries.

    `last_price[asset]`        : most recent value (matches Polymarket's resolver).
    `opening_at(asset, ts)`    : Chainlink value at-or-just-after the given round
                                  start unix-second timestamp; None if not yet seen.
    """

    def __init__(self) -> None:
        self.last_price: dict[str, float] = {}
        self.last_ts_ms: dict[str, int] = {}
        # Per-asset rolling history of (ts_ms, value) — last few minutes.
        self._history: dict[str, deque[tuple[int, float]]] = {
            a: deque(maxlen=2000) for a in SYMBOL_TO_ASSET.values()
        }
        # Cache of resolved openings: (asset, round_start_sec) -> value
        self._openings: dict[tuple[str, int], float] = {}

    def opening_at(self, asset: str, round_start_sec: int) -> float | None:
        """Return Chainlink value at-or-just-after round_start_sec for asset.
        Polymarket's resolver picks the first observation with ts >= boundary,
        so we mirror that.
        """
        key = (asset, round_start_sec)
        cached = self._openings.get(key)
        if cached is not None:
            return cached
        target_ms = round_start_sec * 1000
        for ts_ms, val in self._history.get(asset, ()):
            if ts_ms >= target_ms:
                self._openings[key] = val
                return val
        return None

    @retry(wait=wait_exponential(multiplier=0.5, min=1, max=30), stop=stop_never)
    async def run(self) -> None:
        log.info("chainlink.connect", url=URL)
        async with websockets.connect(URL, ping_interval=20, ping_timeout=15) as ws:
            await ws.send(json.dumps(SUBSCRIBE))
            log.info("chainlink.subscribed")
            async for raw in ws:
                # Server may send PONG/ack frames or actual updates.
                try:
                    msg = orjson.loads(raw)
                except (ValueError, TypeError):
                    continue
                if msg.get("topic") != "crypto_prices_chainlink":
                    continue
                payload = msg.get("payload") or {}
                sym = (payload.get("symbol") or "").lower()
                asset = SYMBOL_TO_ASSET.get(sym)
                if asset is None:
                    continue
                try:
                    val = float(payload["value"])
                    ts_ms = int(payload["timestamp"])
                except (KeyError, TypeError, ValueError):
                    continue
                self.last_price[asset] = val
                self.last_ts_ms[asset] = ts_ms
                self._history[asset].append((ts_ms, val))
