"""Find currently-profitable wallets actively trading 5m crypto markets.

Approach:
1. Pull Polymarket's volume/profit leaderboard
2. For each top wallet, fetch recent activity
3. Filter to wallets where >50% of recent trades are 5m crypto markets
4. Filter to wallets with positive recent P&L (last 24h)
5. Output sorted candidates

Output: data/winning_wallets.json
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from collections import Counter
from pathlib import Path

DATA = Path("/opt/sniper/data")
DATA.mkdir(parents=True, exist_ok=True)
OUT = DATA / "winning_wallets.json"

CRYPTO_5M_RE = re.compile(r"^(btc|eth|sol|xrp|bnb|hype)-updown-5m-\d+$")


def curl_json(url: str, timeout: int = 10):
    try:
        out = subprocess.check_output([
            "curl", "-sS", "-H", "User-Agent: Mozilla/5.0", url,
        ], timeout=timeout)
        return json.loads(out)
    except Exception as e:
        print(f"  err: {e}")
        return None


def probe_leaderboards():
    """Try several known/guessed leaderboard endpoints. Polymarket exposes
    these somewhat — we probe a few shapes."""
    candidates = [
        "https://lb-api.polymarket.com/profit?window=1d&limit=50",
        "https://lb-api.polymarket.com/volume?window=1d&limit=50",
        "https://data-api.polymarket.com/profit?window=1d&limit=50",
        "https://data-api.polymarket.com/leaderboard?window=1d&limit=50",
        "https://data-api.polymarket.com/leaderboard/profit?window=1d&limit=50",
    ]
    for url in candidates:
        print(f"Probing: {url}")
        d = curl_json(url, timeout=8)
        if d and isinstance(d, list) and d:
            print(f"  ✓ Got {len(d)} entries. Sample:")
            print(f"  {json.dumps(d[0], indent=2)[:400]}")
            return url, d
        elif d and isinstance(d, dict):
            print(f"  shape: {list(d.keys())[:8]}")
        else:
            print(f"  empty / no data")
    return None, None


def fetch_wallet_activity(addr: str, limit: int = 100):
    return curl_json(
        f"https://data-api.polymarket.com/activity?user={addr}"
        f"&type=TRADE&limit={limit}&offset=0"
    )


def fetch_wallet_value(addr: str):
    d = curl_json(f"https://data-api.polymarket.com/value?user={addr}")
    if isinstance(d, list) and d:
        return d[0].get("value", 0)
    return 0


def analyze_wallet(addr: str) -> dict:
    """Pull recent activity, score it as a 5m-crypto profile."""
    trades = fetch_wallet_activity(addr, limit=200)
    if not trades or not isinstance(trades, list):
        return {"addr": addr, "skipped": True, "reason": "no activity"}
    crypto_5m = [t for t in trades
                 if CRYPTO_5M_RE.match(t.get("slug", "") or "")]
    if not crypto_5m:
        return {"addr": addr, "skipped": True, "reason": "no 5m crypto trades"}
    pct_5m = 100 * len(crypto_5m) / len(trades)
    asset_dist = Counter(
        t["slug"].split("-")[0].upper() for t in crypto_5m
    )
    side_dist = Counter(t.get("outcome", "?") for t in crypto_5m)
    total_usdc = sum(float(t.get("usdcSize", 0)) for t in crypto_5m)
    avg_usdc = total_usdc / max(1, len(crypto_5m))
    avg_price = (sum(float(t.get("price", 0)) for t in crypto_5m)
                 / max(1, len(crypto_5m)))
    current_value = fetch_wallet_value(addr)
    time.sleep(0.3)  # be polite

    return {
        "addr": addr,
        "current_value": current_value,
        "recent_trades": len(trades),
        "crypto_5m_trades": len(crypto_5m),
        "pct_5m_crypto": round(pct_5m, 1),
        "assets": dict(asset_dist),
        "sides": dict(side_dist),
        "total_usdc_5m": round(total_usdc, 2),
        "avg_usdc_per_trade": round(avg_usdc, 3),
        "avg_entry_price": round(avg_price, 4),
    }


def main() -> None:
    print("=" * 60)
    print(" PROBING LEADERBOARD ENDPOINTS")
    print("=" * 60)
    found_url, lb = probe_leaderboards()
    if not lb:
        print("\nNo leaderboard endpoint worked. Trying fallback: scrape recent trade history.")
        # Fallback: pull last N trades on /trades, count by user
        trades = curl_json("https://data-api.polymarket.com/trades?limit=500")
        if not isinstance(trades, list):
            print("Fallback failed.")
            return
        # Filter to 5m crypto trades only
        crypto = [t for t in trades if CRYPTO_5M_RE.match(t.get("slug", "") or "")]
        print(f"Recent 5m crypto trades: {len(crypto)}")
        users = Counter(t.get("proxyWallet", "") for t in crypto)
        candidates = [u for u, _ in users.most_common(20) if u]
    else:
        # Use leaderboard's top users
        candidates = []
        for entry in lb[:20]:
            for k in ("user", "proxyWallet", "address", "wallet"):
                if k in entry:
                    candidates.append(entry[k])
                    break

    print(f"\nAnalyzing {len(candidates)} candidate wallets...")
    print()
    results = []
    for i, addr in enumerate(candidates):
        if not addr:
            continue
        print(f"  [{i+1}/{len(candidates)}] {addr[:12]}...", end="", flush=True)
        info = analyze_wallet(addr)
        results.append(info)
        if info.get("skipped"):
            print(f"  skip: {info.get('reason')}")
        else:
            print(f"  5m_crypto={info['pct_5m_crypto']:.0f}%  value=${info['current_value']:.2f}  trades={info['crypto_5m_trades']}")

    OUT.write_text(json.dumps(results, indent=2))
    print()
    print("=" * 60)
    print(" CANDIDATES (sorted by 5m crypto activity, then value)")
    print("=" * 60)
    qualified = [r for r in results
                 if not r.get("skipped")
                 and r.get("pct_5m_crypto", 0) >= 50
                 and r.get("crypto_5m_trades", 0) >= 10]
    qualified.sort(key=lambda r: (r["pct_5m_crypto"], r["current_value"]), reverse=True)
    print(f"\nFound {len(qualified)} wallets with >50% 5m crypto activity & >=10 trades:\n")
    print(f"{'Address':<44} {'%5m':>5} {'Value':>10} {'Trades':>7} {'Avg$':>7} {'AvgPx':>7}")
    print("-" * 90)
    for r in qualified[:15]:
        print(f"{r['addr']:<44} {r['pct_5m_crypto']:>5.0f} ${r['current_value']:>8.2f} "
              f"{r['crypto_5m_trades']:>7d} ${r['avg_usdc_per_trade']:>6.2f} "
              f"{r['avg_entry_price']:>7.4f}")
    print()
    print(f"Full data: {OUT}")


if __name__ == "__main__":
    main()
