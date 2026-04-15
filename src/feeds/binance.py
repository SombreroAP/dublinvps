"""Binance WebSocket trade feed for BTC/ETH/SOL.

Maintains a `last_price` dict updated from the bookTicker stream (best bid/ask
mid). Bookticker is push-on-change — lowest latency Binance offers without
paying for direct market data.

Reconnects with backoff. Pings are handled by the websockets library + Binance
sends pings every 3 minutes.
"""
from __future__ import annotations

import asyncio
from typing import Final

import orjson
import websockets
from tenacity import retry, stop_never, wait_exponential

from src.logging_setup import log

WS_URL: Final = "wss://stream.binance.com:9443/stream?streams={streams}"
SYMBOL_MAP: Final = {"BTC": "btcusdt", "ETH": "ethusdt", "SOL": "solusdt"}


class BinanceFeed:
    def __init__(self, assets: list[str] | None = None) -> None:
        self.assets = assets or ["BTC", "ETH", "SOL"]
        self.last_price: dict[str, float] = {}
        self.last_update_ts: dict[str, float] = {}

    @retry(wait=wait_exponential(multiplier=0.5, min=1, max=30), stop=stop_never)
    async def run(self) -> None:
        streams = "/".join(f"{SYMBOL_MAP[a]}@bookTicker" for a in self.assets)
        url = WS_URL.format(streams=streams)
        log.info("binance.connect", url=url)
        async with websockets.connect(url, ping_interval=20, ping_timeout=15) as ws:
            async for raw in ws:
                msg = orjson.loads(raw)
                data = msg.get("data", {})
                sym = data.get("s", "").lower()
                bid = data.get("b")
                ask = data.get("a")
                if not (sym and bid and ask):
                    continue
                asset = next((a for a, s in SYMBOL_MAP.items() if s == sym), None)
                if asset is None:
                    continue
                mid = (float(bid) + float(ask)) / 2
                self.last_price[asset] = mid
                self.last_update_ts[asset] = asyncio.get_event_loop().time()
