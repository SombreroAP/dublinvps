"""Aggregate Dimpled-Dill's trades into per-round picks.

Input:  data/dd_trades.jsonl
Output: data/dd_rounds.jsonl  (one row per (slug, side) ladder)
        + summary stats to stdout

Each round's record:
{
  "slug": "btc-updown-5m-1776258300",
  "asset": "BTC", "side": "Down",
  "round_start": 1776258000, "round_end": 1776258300,
  "first_trade_ts": 1776258230,    # when they first picked this side
  "first_trade_sec_left": 70,      # T-70s
  "fills": 8, "total_size": 1000.0, "total_usdc": 8.30,
  "avg_price": 0.0083, "min_price": 0.01, "max_price": 0.03,
  "best_price": 0.01,
  "outcome": null,                  # filled in later from Gamma resolution
}
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path

DATA = Path("/opt/sniper/data")
TRADES = DATA / "dd_trades.jsonl"
ROUNDS = DATA / "dd_rounds.jsonl"

SLUG_PATTERN = re.compile(r"^(btc|eth|sol|xrp|bnb|hype)-updown-5m-(\d+)$")
ASSET_MAP = {"btc": "BTC", "eth": "ETH", "sol": "SOL", "xrp": "XRP",
             "bnb": "BNB", "hype": "HYPE"}


def main() -> None:
    if not TRADES.exists():
        print(f"No trades file at {TRADES}. Run dd_scrape.py first.")
        return

    # Group all fills by (slug, side)
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    skipped_non_5m = 0
    for line in TRADES.open():
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        slug = r.get("slug") or ""
        m = SLUG_PATTERN.match(slug)
        if not m:
            skipped_non_5m += 1
            continue
        side = r.get("outcome", "").strip()  # "Up" or "Down"
        if side not in ("Up", "Down"):
            continue
        groups[(slug, side)].append(r)

    rows = []
    for (slug, side), trades in groups.items():
        m = SLUG_PATTERN.match(slug)
        asset_short, ts_str = m.group(1), m.group(2)
        round_start = int(ts_str)
        round_end = round_start + 300
        # Sort by timestamp ascending
        trades.sort(key=lambda x: x.get("timestamp", 0))
        first = trades[0]
        first_ts = int(first.get("timestamp", 0))
        first_sec_left = round_end - first_ts
        prices = [float(t.get("price", 0)) for t in trades if t.get("price")]
        sizes = [float(t.get("size", 0)) for t in trades if t.get("size")]
        usdcs = [float(t.get("usdcSize", 0)) for t in trades if t.get("usdcSize")]
        rows.append({
            "slug": slug,
            "asset": ASSET_MAP.get(asset_short, asset_short.upper()),
            "side": side,
            "round_start": round_start, "round_end": round_end,
            "first_trade_ts": first_ts,
            "first_trade_sec_left": first_sec_left,
            "fills": len(trades),
            "total_size": sum(sizes),
            "total_usdc": sum(usdcs),
            "avg_price": (sum(usdcs) / sum(sizes)) if sum(sizes) else None,
            "min_price": min(prices) if prices else None,
            "max_price": max(prices) if prices else None,
            "best_price": min(prices) if prices else None,
            # Map Up/Down to YES/NO token sides for compatibility
            "yes_or_no": "YES" if side == "Up" else "NO",
        })

    # Sort newest first
    rows.sort(key=lambda r: r["first_trade_ts"], reverse=True)
    with ROUNDS.open("w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")

    # Summary
    by_asset = defaultdict(int)
    by_side = defaultdict(int)
    both_sides = 0
    seen_slugs = set()
    by_slug = defaultdict(set)
    for r in rows:
        by_asset[r["asset"]] += 1
        by_side[r["side"]] += 1
        seen_slugs.add(r["slug"])
        by_slug[r["slug"]].add(r["side"])
    for sides in by_slug.values():
        if "Up" in sides and "Down" in sides:
            both_sides += 1

    print(f"Skipped non-5m: {skipped_non_5m}")
    print(f"Total trades   : {sum(1 for _ in TRADES.open())}")
    print(f"Unique rounds  : {len(seen_slugs)}")
    print(f"Round-side picks: {len(rows)} -> {ROUNDS}")
    print(f"Picks by asset : {dict(by_asset)}")
    print(f"Picks by side  : {dict(by_side)}")
    print(f"Rounds where they bought BOTH sides: {both_sides}/{len(seen_slugs)} ({100*both_sides/max(1,len(seen_slugs)):.1f}%)")
    print()
    sec_lefts = [r["first_trade_sec_left"] for r in rows]
    if sec_lefts:
        sec_lefts.sort()
        n = len(sec_lefts)
        print(f"first_trade_sec_left distribution (T-X seconds):")
        print(f"  min  {sec_lefts[0]:4d}   p10  {sec_lefts[n//10]:4d}   p50  {sec_lefts[n//2]:4d}   p90  {sec_lefts[9*n//10]:4d}   max  {sec_lefts[-1]:4d}")
    print()
    print("Sample 5 most recent picks:")
    for r in rows[:5]:
        print(f"  {r['slug']:35s} side={r['side']:4s}  T-{r['first_trade_sec_left']:3d}s  fills={r['fills']:2d}  ${r['total_usdc']:5.2f}  avgpx={r['avg_price']:.4f}")


if __name__ == "__main__":
    main()
