"""Polymarket RTDS WebSocket — Chainlink price stream.

Subscribes to wss://ws-live-data.polymarket.com → topic `crypto_prices_chainlink`.
This is the EXACT signed price Polymarket's resolver uses for 5m/15m crypto
markets, so it eliminates the basis risk of using Binance as a proxy.

Protocol requires app-level PING every ~5s. Without it, the server stops
pushing updates while leaving the TCP alive (silent staleness). We also run
a watchdog that force-reconnects if no update arrives for STALENESS_LIMIT.
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
PING_INTERVAL_SEC: Final = 5.0
STALENESS_LIMIT_SEC: Final = 30.0


class _StalenessError(Exception):
    """Raised by watchdog to force reconnect via tenacity."""


class ChainlinkFeed:
    def __init__(self) -> None:
        self.last_price: dict[str, float] = {}
        self.last_ts_ms: dict[str, int] = {}
        self._last_msg_mono: float = 0.0  # monotonic clock of last received message
        self._history: dict[str, deque[tuple[int, float]]] = {
            a: deque(maxlen=2000) for a in SYMBOL_TO_ASSET.values()
        }
        self._openings: dict[tuple[str, int], float] = {}

    def opening_at(self, asset: str, round_start_sec: int) -> float | None:
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

    def velocity_bps_per_sec(self, asset: str, lookback_sec: float = 5.0
                              ) -> float | None:
        """Rate of price change over the last `lookback_sec`, in bps/sec.
        Positive = rising, negative = falling. None if not enough history.

        Uses first observation ≥ (now - lookback_sec) as the reference point.
        That way it's robust to uneven Chainlink cadence.
        """
        hist = self._history.get(asset)
        if not hist or len(hist) < 2:
            return None
        now_ms = hist[-1][0]
        current_val = hist[-1][1]
        cutoff_ms = now_ms - int(lookback_sec * 1000)
        ref_ts_ms = None
        ref_val = None
        # Walk from oldest → newest, pick first sample at/after cutoff.
        for ts_ms, val in hist:
            if ts_ms >= cutoff_ms:
                ref_ts_ms = ts_ms
                ref_val = val
                break
        if ref_ts_ms is None or ref_val is None or ref_val <= 0:
            return None
        dt_sec = (now_ms - ref_ts_ms) / 1000.0
        if dt_sec <= 0:
            return None
        # (current - ref) / ref * 10000 = move in bps; / dt = bps/sec.
        return (current_val - ref_val) / ref_val * 10_000 / dt_sec

    async def _pinger(self, ws: "websockets.WebSocketClientProtocol") -> None:
        while True:
            await asyncio.sleep(PING_INTERVAL_SEC)
            try:
                await ws.send("PING")
            except websockets.ConnectionClosed:
                return

    async def _watchdog(self) -> None:
        """Force reconnect if we haven't seen a message in STALENESS_LIMIT_SEC."""
        while True:
            await asyncio.sleep(5.0)
            if self._last_msg_mono and (time.monotonic() - self._last_msg_mono) > STALENESS_LIMIT_SEC:
                log.error("chainlink.stale_reconnect",
                          stale_for_sec=time.monotonic() - self._last_msg_mono)
                raise _StalenessError()

    async def _receiver(self, ws: "websockets.WebSocketClientProtocol") -> None:
        async for raw in ws:
            if isinstance(raw, bytes):
                continue
            if raw == "PONG":
                self._last_msg_mono = time.monotonic()
                continue
            try:
                msg = orjson.loads(raw)
            except (ValueError, TypeError):
                continue
            if msg.get("topic") != "crypto_prices_chainlink":
                self._last_msg_mono = time.monotonic()
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
            self._last_msg_mono = time.monotonic()
            self._history[asset].append((ts_ms, val))

    @retry(wait=wait_exponential(multiplier=0.5, min=1, max=30), stop=stop_never)
    async def run(self) -> None:
        log.info("chainlink.connect", url=URL)
        async with websockets.connect(URL, ping_interval=20, ping_timeout=15,
                                       open_timeout=10) as ws:
            await ws.send(json.dumps(SUBSCRIBE))
            log.info("chainlink.subscribed")
            self._last_msg_mono = time.monotonic()
            # Run receiver + pinger + watchdog concurrently. First to fail
            # (or watchdog tripping) cancels the others and re-raises so
            # tenacity reconnects.
            tasks = [
                asyncio.create_task(self._receiver(ws)),
                asyncio.create_task(self._pinger(ws)),
                asyncio.create_task(self._watchdog()),
            ]
            try:
                done, pending = await asyncio.wait(
                    tasks, return_when=asyncio.FIRST_EXCEPTION
                )
                for t in pending:
                    t.cancel()
                for t in done:
                    exc = t.exception()
                    if exc:
                        raise exc
            finally:
                for t in tasks:
                    if not t.done():
                        t.cancel()
