"""Phase 2: Pull Binance historical prices around each round_start and
compute candidate features for predicting Dimpled-Dill's side choice.

Inputs:  data/dd_rounds.jsonl
Outputs: data/dd_features.jsonl

For each (slug, side) pick, fetches Binance 1-second klines for the asset's
USDT pair around round_start, computes:
  - price_at_start
  - price_60s_before  - price_300s_before
  - mom_1m_bps  : (start - 60sBefore) / 60sBefore * 10000
  - mom_5m_bps  : (start - 300sBefore) / 300sBefore * 10000
  - vol_1m_bps  : stddev of 1-second returns over last 60s
  - hour_utc    : 0-23
  - day_of_week : 0-6 (Mon=0)
"""
from __future__ import annotations

import json
import statistics
import subprocess
import time
from pathlib import Path
from datetime import datetime, timezone

DATA = Path("/opt/sniper/data")
ROUNDS = DATA / "dd_rounds.jsonl"
FEATURES = DATA / "dd_features.jsonl"

BINANCE_KLINE = "https://api.binance.com/api/v3/klines"
ASSET_TO_PAIR = {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT",
                 "XRP": "XRPUSDT", "BNB": "BNBUSDT"}


def fetch_klines(pair: str, start_ms: int, end_ms: int, interval: str = "1s") -> list[list]:
    """Binance klines: [[openTime, open, high, low, close, volume, ...], ...]"""
    url = (f"{BINANCE_KLINE}?symbol={pair}&interval={interval}"
           f"&startTime={start_ms}&endTime={end_ms}&limit=1000")
    out = subprocess.check_output([
        "curl", "-sS", "-H", "User-Agent: Mozilla/5.0", url,
    ], timeout=10)
    return json.loads(out)


def price_at_or_before(klines: list[list], target_ms: int) -> float | None:
    """Return close price of the latest kline whose openTime <= target_ms."""
    best = None
    for k in klines:
        open_ms = int(k[0])
        if open_ms > target_ms:
            break
        best = float(k[4])  # close
    return best


def main() -> None:
    if not ROUNDS.exists():
        print(f"Run dd_aggregate.py first ({ROUNDS} missing).")
        return

    rounds = [json.loads(l) for l in ROUNDS.open()]
    print(f"Loaded {len(rounds)} round picks")

    # Cache klines per (asset, hour) to avoid re-fetching
    kline_cache: dict[tuple[str, int], list[list]] = {}
    out_rows = []
    skipped = 0
    for i, r in enumerate(rounds):
        asset = r["asset"]
        pair = ASSET_TO_PAIR.get(asset)
        if not pair:
            skipped += 1
            continue
        round_start = r["round_start"]
        # Window: 6 minutes before to round start
        window_start_ms = (round_start - 360) * 1000
        window_end_ms = (round_start + 1) * 1000
        cache_key = (asset, round_start // 600)  # 10-min cache buckets
        if cache_key not in kline_cache:
            try:
                kline_cache[cache_key] = fetch_klines(pair, window_start_ms, window_end_ms, "1s")
            except subprocess.CalledProcessError:
                kline_cache[cache_key] = []
            time.sleep(0.05)  # be polite
        klines = kline_cache[cache_key]
        if not klines:
            skipped += 1
            continue

        p_start = price_at_or_before(klines, round_start * 1000)
        p_60 = price_at_or_before(klines, (round_start - 60) * 1000)
        p_300 = price_at_or_before(klines, (round_start - 300) * 1000)

        if p_start is None or p_60 is None:
            skipped += 1
            continue

        # 1-second returns over last 60s for vol
        last_60s = [k for k in klines if (round_start - 60) * 1000 <= int(k[0]) <= round_start * 1000]
        rets = []
        for j in range(1, len(last_60s)):
            prev = float(last_60s[j-1][4])
            cur = float(last_60s[j][4])
            if prev > 0:
                rets.append((cur - prev) / prev)
        vol_1m_bps = (statistics.pstdev(rets) * 10_000) if len(rets) > 1 else 0.0

        mom_1m_bps = (p_start - p_60) / p_60 * 10_000
        mom_5m_bps = ((p_start - p_300) / p_300 * 10_000) if p_300 else None

        dt = datetime.fromtimestamp(round_start, timezone.utc)
        out_rows.append({
            **r,
            "p_start": p_start, "p_60s_before": p_60, "p_300s_before": p_300,
            "mom_1m_bps": mom_1m_bps, "mom_5m_bps": mom_5m_bps,
            "vol_1m_bps": vol_1m_bps,
            "hour_utc": dt.hour, "day_of_week": dt.weekday(),
        })

        if (i + 1) % 25 == 0:
            print(f"  processed {i+1}/{len(rounds)} (skipped so far: {skipped})")

    with FEATURES.open("w") as f:
        for r in out_rows:
            f.write(json.dumps(r) + "\n")

    print()
    print(f"Wrote {len(out_rows)} feature rows to {FEATURES}  (skipped {skipped})")
    print()
    # Quick sanity stats
    ups = [r for r in out_rows if r["side"] == "Up"]
    downs = [r for r in out_rows if r["side"] == "Down"]
    if ups and downs:
        avg_mom_ups = statistics.mean(r["mom_1m_bps"] for r in ups)
        avg_mom_downs = statistics.mean(r["mom_1m_bps"] for r in downs)
        print(f"Avg 1m momentum (bps) preceding their pick:")
        print(f"  Up picks   : {avg_mom_ups:+.2f} bps  (n={len(ups)})")
        print(f"  Down picks : {avg_mom_downs:+.2f} bps  (n={len(downs)})")
        if avg_mom_ups > avg_mom_downs:
            print("  → They tend to bet UP after positive momentum (TREND-following)")
        else:
            print("  → They tend to bet UP after negative momentum (FADE / mean-revert)")


if __name__ == "__main__":
    main()
