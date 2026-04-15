"""Multi-wallet shadow bot — mirror multiple Polymarket traders in parallel.

Polls /activity?user=<addr> for each tracked wallet. New BUY trades on 5m
crypto markets are mirrored as paper signals to a per-wallet log file.

Output: paper_trades_shadow_<label>.jsonl  (one per wallet)
        shadow_seen_<label>.txt            (dedup state per wallet)
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

# Tracked wallets. Add/remove here.
# label is short alphanum used in log filenames.
WALLETS: dict[str, str] = {
    "top65k":  "0xeebde7a0e019a63e6b476eb425505b7b3e6eba30",
    "mid10k":  "0x3a847382ad6fff9be1db4e073fd9b869f6884d44",
    "dd":      "0x7Da07B2a8B009A406198677dEBDA46ad651B6bE2",
}

POLL_SEC = 5.0
BASE = Path("/opt/sniper")
SLUG_RE = re.compile(r"^(btc|eth|sol|xrp|bnb|hype)-updown-5m-(\d+)$")


def _paper_log(label: str) -> Path:
    return BASE / f"paper_trades_shadow_{label}.jsonl"


def _seen_log(label: str) -> Path:
    return BASE / f"shadow_seen_{label}.txt"


def fetch_recent_trades(addr: str) -> list[dict]:
    try:
        out = subprocess.check_output([
            "curl", "-sS", "-H", "User-Agent: Mozilla/5.0",
            f"https://data-api.polymarket.com/activity"
            f"?user={addr}&type=TRADE&limit=100&offset=0",
        ], timeout=10)
        d = json.loads(out)
        if isinstance(d, list):
            return d
    except Exception as e:
        log.error("shadow.fetch.error", addr=addr[:10], error=str(e))
    return []


def load_seen(label: str) -> set[str]:
    f = _seen_log(label)
    if not f.exists():
        return set()
    return {line.strip() for line in f.open() if line.strip()}


def append_seen(label: str, hashes: list[str]) -> None:
    with _seen_log(label).open("a") as f:
        for h in hashes:
            f.write(h + "\n")


async def poll_wallet(label: str, addr: str) -> None:
    """One poll loop per wallet; runs concurrently with the others."""
    seen = load_seen(label)
    log.info("shadow.start", label=label, addr=addr[:10], seen_count=len(seen))
    while True:
        trades = fetch_recent_trades(addr)
        new_buys: list[dict] = []
        new_hashes: list[str] = []
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
            append_seen(label, new_hashes)

        for t in new_buys:
            slug = t["slug"]
            asset = slug.split("-")[0].upper()
            side = "YES" if t["outcome"] == "Up" else "NO"
            try:
                round_start = int(slug.split("-")[-1])
            except ValueError:
                continue
            ts = int(t.get("timestamp", time.time()))
            sig = {
                "ts": ts, "wallet_label": label,
                "slug": slug, "asset": asset, "side": side,
                "dd_price": float(t.get("price", 0)),
                "dd_size_shares": float(t.get("size", 0)),
                "dd_usdc": float(t.get("usdcSize", 0)),
                "sec_left": round_start + 300 - ts,
                "round_start": round_start,
                "round_end": round_start + 300,
                "tx_hash": t.get("transactionHash"),
                "log_index": t.get("logIndex"),
                "levels": [[float(t.get("price", 0)), float(t.get("usdcSize", 0))]],
                "total_usdc": float(t.get("usdcSize", 0)),
                "strategy": f"shadow_{label}_v1",
            }
            _paper_log(label).open("ab").write(orjson.dumps(sig) + b"\n")
            log.info("shadow.signal", label=label, slug=slug, asset=asset,
                     side=side, price=t.get("price"), usdc=t.get("usdcSize"))

        await asyncio.sleep(POLL_SEC)


async def main() -> None:
    configure()
    log.info("shadow.startup", wallets=list(WALLETS.keys()), poll_sec=POLL_SEC)
    # Pre-create log files so systemd's ReadWritePaths is happy.
    for label in WALLETS:
        _paper_log(label).touch(exist_ok=True)
        _seen_log(label).touch(exist_ok=True)
    await asyncio.gather(*(poll_wallet(l, a) for l, a in WALLETS.items()))


if __name__ == "__main__":
    asyncio.run(main())
