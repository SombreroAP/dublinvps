"""Scrape Dimpled-Dill's complete Polymarket trade history.

Wallet: 0x7Da07B2a8B009A406198677dEBDA46ad651B6bE2 ("Dimpled-Dill")

Polymarket data API exposes /activity?user=<addr> paginated. The endpoint
caps at ~3000 trades per query window so we paginate with offset until empty.

Output: data/dd_trades.jsonl  (one JSON line per trade, append-safe)
        data/dd_redemptions.jsonl  (REDEEM events = wins)

Usage: python scripts/dd_scrape.py
       (re-run any time; deduplicates by transactionHash + index)
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

WALLET = "0x7Da07B2a8B009A406198677dEBDA46ad651B6bE2"
OUTDIR = Path("/opt/sniper/data")
OUTDIR.mkdir(parents=True, exist_ok=True)
TRADES_LOG = OUTDIR / "dd_trades.jsonl"
REDEEMS_LOG = OUTDIR / "dd_redemptions.jsonl"

API = "https://data-api.polymarket.com/activity"


def _fetch(activity_type: str | None, offset: int, limit: int = 500) -> list[dict]:
    params = f"user={WALLET}&limit={limit}&offset={offset}"
    if activity_type:
        params += f"&type={activity_type}"
    out = subprocess.check_output([
        "curl", "-sS", "-H", "User-Agent: Mozilla/5.0",
        f"{API}?{params}",
    ], timeout=15)
    return json.loads(out)


def _scrape(activity_type: str | None, dest: Path, label: str) -> int:
    seen = set()
    if dest.exists():
        for line in dest.open():
            try:
                r = json.loads(line)
                seen.add(r.get("transactionHash", "") + "|" + str(r.get("logIndex", "")))
            except json.JSONDecodeError:
                continue
    print(f"[{label}] {len(seen)} previously cached")

    new_rows = 0
    offset = 0
    LIMIT = 500
    while True:
        try:
            page = _fetch(activity_type, offset, LIMIT)
        except subprocess.CalledProcessError as e:
            print(f"[{label}] fetch err at offset={offset}: {e}", file=sys.stderr)
            break
        if not page:
            break
        if not isinstance(page, list):
            print(f"[{label}] non-list response at offset={offset}, stopping (likely API cap). raw={str(page)[:120]}")
            break
        added_in_page = 0
        with dest.open("ab") as f:
            for r in page:
                if not isinstance(r, dict):
                    continue
                key = r.get("transactionHash", "") + "|" + str(r.get("logIndex", ""))
                if key in seen:
                    continue
                seen.add(key)
                f.write((json.dumps(r) + "\n").encode())
                added_in_page += 1
        new_rows += added_in_page
        print(f"[{label}] offset={offset:5d}  page={len(page):3d}  new={added_in_page:3d}  total_seen={len(seen)}")
        if len(page) < LIMIT:
            break
        offset += LIMIT
        time.sleep(0.5)  # be nice
    print(f"[{label}] DONE — {new_rows} new rows added to {dest}")
    return new_rows


def main() -> None:
    print(f"Scraping wallet {WALLET}")
    print(f"Output dir: {OUTDIR}")
    print()
    _scrape("TRADE", TRADES_LOG, "trades")
    print()
    _scrape("REDEEM", REDEEMS_LOG, "redeems")
    print()
    # Quick stats
    trades = sum(1 for _ in TRADES_LOG.open()) if TRADES_LOG.exists() else 0
    redeems = sum(1 for _ in REDEEMS_LOG.open()) if REDEEMS_LOG.exists() else 0
    print(f"Total cached: {trades} trades, {redeems} redemptions")


if __name__ == "__main__":
    main()
