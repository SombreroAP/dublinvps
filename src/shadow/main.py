"""Shadow bot — mirror Dimpled-Dill's live trades.

Polls https://data-api.polymarket.com/activity?user=<DD_WALLET> every 5s.
For each NEW BUY trade on a 5m crypto market, logs it as a paper signal in
our format. Backtest uses the realistic fill simulator (same as ladder bot)
to estimate what we would have gotten if we'd placed the same bid 5-10s
later than DD.

Output: paper_trades_shadow.jsonl
"""
from __future__ import annotations

import asyncio
import json
import re
import subprocess
import time
from pathlib import Path

import orjson

from src.logging_setup import configure, log

WALLET = "0x7Da07B2a8B009A406198677dEBDA46ad651B6bE2"
PAPER_LOG = Path("paper_trades_shadow.jsonl")
SEEN_HASHES_LOG = Path("shadow_seen_hashes.txt")
POLL_SEC = 5.0

SLUG_RE = re.compile(r"^(btc|eth|sol|xrp|bnb|hype)-updown-5m-(\d+)$")


def fetch_recent_trades() -> list[dict]:
    try:
        out = subprocess.check_output([
            "curl", "-s", "-H", "User-Agent: Mozilla/5.0",
            f"https://data-api.polymarket.com/activity"
            f"?user={WALLET}&type=TRADE&limit=100&offset=0",
        ], timeout=10)
        d = json.loads(out)
        if isinstance(d, list):
            return d
    except Exception as e:
        log.error("shadow.fetch.error", error=str(e))
    return []


def load_seen() -> set[str]:
    if not SEEN_HASHES_LOG.exists():
        return set()
    return {line.strip() for line in SEEN_HASHES_LOG.open() if line.strip()}


def append_seen(hashes: list[str]) -> None:
    with SEEN_HASHES_LOG.open("a") as f:
        for h in hashes:
            f.write(h + "\n")


async def main() -> None:
    configure()
    log.info("shadow.startup", wallet=WALLET, poll_sec=POLL_SEC)
    seen = load_seen()
    log.info("shadow.cache_loaded", seen_count=len(seen))

    while True:
        trades = fetch_recent_trades()
        new_buys = []
        new_hashes = []
        for t in trades:
            h = (t.get("transactionHash", "") + "|" +
                 str(t.get("logIndex", "")))
            if h in seen:
                continue
            seen.add(h)
            new_hashes.append(h)
            slug = t.get("slug") or ""
            if not SLUG_RE.match(slug):
                continue
            if t.get("side") != "BUY":
                continue
            outcome = t.get("outcome", "")
            if outcome not in ("Up", "Down"):
                continue
            new_buys.append(t)

        if new_hashes:
            append_seen(new_hashes)

        for t in new_buys:
            slug = t["slug"]
            asset = slug.split("-")[0].upper()
            side = "YES" if t["outcome"] == "Up" else "NO"
            try:
                round_start = int(slug.split("-")[-1])
            except ValueError:
                continue
            ts = int(t.get("timestamp", time.time()))
            sec_left = round_start + 300 - ts
            sig = {
                "ts": ts,
                "slug": slug, "asset": asset, "side": side,
                "dd_price": float(t.get("price", 0)),
                "dd_size_shares": float(t.get("size", 0)),
                "dd_usdc": float(t.get("usdcSize", 0)),
                "sec_left": sec_left,
                "round_start": round_start,
                "round_end": round_start + 300,
                "tx_hash": t.get("transactionHash"),
                "log_index": t.get("logIndex"),
                # We treat each DD fill as a hint to mirror — same price + size.
                "levels": [[float(t.get("price", 0)), float(t.get("usdcSize", 0))]],
                "total_usdc": float(t.get("usdcSize", 0)),
                "strategy": "shadow_dd_v1",
            }
            PAPER_LOG.open("ab").write(orjson.dumps(sig) + b"\n")
            log.info("shadow.signal", slug=slug, asset=asset, side=side,
                     price=t.get("price"), usdc=t.get("usdcSize"),
                     sec_left=sec_left)

        await asyncio.sleep(POLL_SEC)


if __name__ == "__main__":
    asyncio.run(main())
