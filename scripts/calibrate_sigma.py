"""Calibrate per-√sec log-price volatility for BTC/ETH/SOL.

Method:
1. Pull ~7 days of 1-minute klines from Binance (free, no auth).
2. For each asset, compute rolling 5-minute log returns: log(close_t5 / close_t0).
3. Std dev of those returns = σ_5min (realized).
4. σ_per_√sec = σ_5min / √300.
5. Convert to bps: × 10_000.

The bot uses this σ in:
    fair_p = Φ(move_bps / (σ_bps · √seconds_left))

Run:  /opt/sniper/.venv/bin/python /opt/sniper/scripts/calibrate_sigma.py

Output: recommended SIGMA_BPS_* values + a summary of the distributions.
"""
from __future__ import annotations

import json
import math
import subprocess
import sys
from statistics import mean, stdev

SYMBOLS = {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT"}
BINANCE = "https://api.binance.com/api/v3/klines"
LOOKBACK_DAYS = 7
# Binance gives max 1000 bars per call; 7 days of 1m = 10080 bars → 11 calls
CHUNK_SIZE = 1000
MINUTES_PER_DAY = 60 * 24


def fetch(symbol: str, days: int) -> list[list]:
    """Fetch 1-minute klines for `days` back. Returns list of [open_time, open,
    high, low, close, volume, close_time, ...]."""
    end_ms = int(subprocess.check_output(["date", "+%s000"]).strip())
    start_ms = end_ms - days * 24 * 3600 * 1000
    out = []
    cursor = start_ms
    while cursor < end_ms:
        url = (f"{BINANCE}?symbol={symbol}&interval=1m"
               f"&startTime={cursor}&limit={CHUNK_SIZE}")
        raw = subprocess.check_output(
            ["curl", "-sS", "-H", "User-Agent: calibrate/1.0", url], timeout=20
        )
        batch = json.loads(raw)
        if not batch:
            break
        out.extend(batch)
        # Advance cursor past the last bar (open_time)
        cursor = int(batch[-1][0]) + 60_000
        if len(batch) < CHUNK_SIZE:
            break
    return out


def rolling_5min_returns(bars: list[list]) -> list[float]:
    """Return log(close_{t+5} / close_t) for every minute t where both exist."""
    closes = [float(b[4]) for b in bars]
    out = []
    for i in range(len(closes) - 5):
        c0, c5 = closes[i], closes[i + 5]
        if c0 > 0 and c5 > 0:
            out.append(math.log(c5 / c0))
    return out


def summarize(asset: str, returns: list[float]) -> dict:
    if len(returns) < 30:
        return {"asset": asset, "error": "too few returns"}
    sigma_5min = stdev(returns)
    sigma_per_sqrt_sec = sigma_5min / math.sqrt(300)
    sigma_bps_per_sqrt_sec = sigma_per_sqrt_sec * 10_000

    # Distribution of |move| in bps at 5m horizon
    mags = [abs(r) * 10_000 for r in returns]
    mags.sort()
    def pct(p): return mags[int(len(mags) * p)]

    return {
        "asset": asset,
        "samples": len(returns),
        "sigma_5min_bps": sigma_5min * 10_000,
        "sigma_bps_per_sqrt_sec": sigma_bps_per_sqrt_sec,
        "median_5min_move_bps": pct(0.5),
        "p90_5min_move_bps": pct(0.9),
        "p99_5min_move_bps": pct(0.99),
        "max_5min_move_bps": mags[-1],
    }


def main() -> None:
    print(f"Calibrating σ from {LOOKBACK_DAYS} days of Binance 1m klines\n")
    results = {}
    for asset, symbol in SYMBOLS.items():
        print(f"{asset} ({symbol})... ", end="", flush=True)
        bars = fetch(symbol, LOOKBACK_DAYS)
        returns = rolling_5min_returns(bars)
        r = summarize(asset, returns)
        results[asset] = r
        if "error" in r:
            print(f"ERROR: {r['error']}")
            continue
        print(f"{len(bars)} bars → {len(returns)} rolling 5m returns")
        print(f"   σ_5min:                 {r['sigma_5min_bps']:.2f} bps")
        print(f"   σ per √sec:             {r['sigma_bps_per_sqrt_sec']:.3f} bps")
        print(f"   median |5m move|:       {r['median_5min_move_bps']:.2f} bps")
        print(f"   p90 |5m move|:          {r['p90_5min_move_bps']:.2f} bps")
        print(f"   p99 |5m move|:          {r['p99_5min_move_bps']:.2f} bps")
        print(f"   max |5m move|:          {r['max_5min_move_bps']:.1f} bps")
        print()

    print("=" * 60)
    print("Recommended .env values (paste into /opt/sniper/.env):")
    print("=" * 60)
    for asset, r in results.items():
        if "error" not in r:
            # Round to 2 decimals, ensure a minimum floor to avoid div-by-zero
            sigma = max(0.1, round(r['sigma_bps_per_sqrt_sec'], 2))
            print(f"SIGMA_BPS_{asset}={sigma}")
    print("\nAfter editing .env:  systemctl restart sniper dashboard")
    print()
    print("Current .env values for comparison:")
    try:
        with open("/opt/sniper/.env") as f:
            for line in f:
                if line.startswith("SIGMA_BPS_"):
                    print("  " + line.rstrip())
    except FileNotFoundError:
        pass


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
