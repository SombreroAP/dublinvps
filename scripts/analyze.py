"""Comprehensive analysis of paper_trades.jsonl vs actual market resolutions.

Slices signals by asset, side, fair_p bucket, ask-price bucket, seconds-left
bucket, disagreement bucket. Shows win rate + P&L per slice.

Usage: /opt/sniper/.venv/bin/python /opt/sniper/scripts/analyze.py
"""
from __future__ import annotations

import json
import subprocess
from collections import defaultdict
from pathlib import Path
from statistics import mean, median

CHAINLINK_CUTOFF = 1776250620  # systemd go-live
PAPER_LOG = Path("/opt/sniper/paper_trades.jsonl")


def fetch_resolution(slug: str) -> str:
    try:
        raw = subprocess.check_output([
            "curl", "-s", "-H", "User-Agent: Mozilla/5.0",
            f"https://gamma-api.polymarket.com/events?slug={slug}",
        ], timeout=6)
        d = json.loads(raw)
        if not d:
            return "notfound"
        m = d[0].get("markets", [{}])[0]
        if not m.get("closed"):
            return "open"
        op = json.loads(m.get("outcomePrices") or "[]")
        if op == ["1", "0"]: return "UP"
        if op == ["0", "1"]: return "DOWN"
    except Exception:
        return "err"
    return "unknown"


def compute_pl(r: dict, won: bool) -> float:
    shares = r["size_usdc"] / r["ask"] if r["ask"] > 0 else 0
    payoff = shares * (1.0 if won else 0.0)
    fee = r.get("fee", 0.0)
    return payoff - r["size_usdc"] - r["size_usdc"] * fee


def bucket_report(title: str, by_bucket: dict) -> None:
    print(f"\n== {title} ==")
    print(f"  {'bucket':<20} {'n':>4} {'wins':>5} {'win%':>6} {'avg_edge':>10} {'tot_pl':>10}")
    for key, picks in sorted(by_bucket.items()):
        decided = [p for p in picks if p["result"] in ("WIN", "LOSS")]
        wins = sum(1 for p in decided if p["result"] == "WIN")
        wr = (wins / len(decided) * 100) if decided else 0.0
        edges = [p["edge"] for p in picks]
        tot_pl = sum(p.get("pl", 0.0) for p in picks)
        print(f"  {str(key):<20} {len(picks):>4} {wins:>5} {wr:>5.1f}%  "
              f"{mean(edges)*100:>8.1f}%  ${tot_pl:>+8.2f}")


def main() -> None:
    if not PAPER_LOG.exists():
        print("no paper log"); return

    rows = [json.loads(l) for l in PAPER_LOG.open()]
    post = [r for r in rows if r["ts"] >= CHAINLINK_CUTOFF]

    # Dedup to best-edge per (slug, side)
    best: dict = {}
    for r in post:
        k = (r["slug"], r["side"])
        if k not in best or r["edge"] > best[k]["edge"]:
            best[k] = r
    picks = list(best.values())

    print("=" * 70)
    print(f"TOTAL SIGNALS: {len(rows)}  |  post-Chainlink: {len(post)}  |  unique picks: {len(picks)}")
    print("=" * 70)

    # Resolve each
    print("\nResolving outcomes...", flush=True)
    for p in picks:
        res = fetch_resolution(p["slug"])
        if res in ("UP", "DOWN"):
            won = (p["side"] == "YES" and res == "UP") or (p["side"] == "NO" and res == "DOWN")
            p["result"] = "WIN" if won else "LOSS"
            p["pl"] = compute_pl(p, won)
        else:
            p["result"] = res
            p["pl"] = 0.0

    decided = [p for p in picks if p["result"] in ("WIN", "LOSS")]
    undecided = [p for p in picks if p["result"] not in ("WIN", "LOSS")]

    print(f"\nResolved: {len(decided)}  (still pending/err: {len(undecided)})")
    if not decided:
        print("No resolved picks yet."); return

    wins = sum(1 for p in decided if p["result"] == "WIN")
    losses = len(decided) - wins
    tot_pl = sum(p["pl"] for p in decided)
    print(f"W/L: {wins}/{losses}  |  win rate: {wins/len(decided)*100:.1f}%")
    print(f"Total P&L on resolved: ${tot_pl:+.2f}")
    print(f"Avg P&L per pick: ${tot_pl/len(decided):+.2f}")

    # --- Slices ---
    by_asset = defaultdict(list)
    for p in decided: by_asset[p["asset"]].append(p)
    bucket_report("By asset", by_asset)

    by_side = defaultdict(list)
    for p in decided: by_side[p["side"]].append(p)
    bucket_report("By side", by_side)

    # fair_p buckets: [0.5, 0.6), [0.6, 0.7), ..., [0.9, 1.0]
    by_fp = defaultdict(list)
    for p in decided:
        fp = p["fair_p"]
        b = f"[{int(fp*10)/10:.1f}-{int(fp*10)/10+0.1:.1f})" if fp < 1.0 else "[0.9-1.0]"
        by_fp[b].append(p)
    bucket_report("By fair_p bucket", by_fp)

    # Ask price buckets
    by_ask = defaultdict(list)
    for p in decided:
        a = p["ask"]
        if a < 0.05: b = "<0.05"
        elif a < 0.20: b = "0.05-0.20"
        elif a < 0.40: b = "0.20-0.40"
        elif a < 0.60: b = "0.40-0.60"
        else: b = ">=0.60"
        by_ask[b].append(p)
    bucket_report("By ask price bucket", by_ask)

    # Seconds-left at signal (timing)
    by_sl = defaultdict(list)
    for p in decided:
        sl = p.get("sec_left", 0)
        if sl > 30: b = "30-45"
        elif sl > 20: b = "20-30"
        elif sl > 10: b = "10-20"
        else: b = "<10"
        by_sl[b].append(p)
    bucket_report("By seconds-left at signal", by_sl)

    # Disagreement bucket (only signals that have this field, i.e. post-filter)
    picks_with_dis = [p for p in decided if "disagreement" in p]
    if picks_with_dis:
        by_dis = defaultdict(list)
        for p in picks_with_dis:
            d = p["disagreement"]
            if d < 0.05: b = "<0.05"
            elif d < 0.10: b = "0.05-0.10"
            elif d < 0.20: b = "0.10-0.20"
            elif d < 0.30: b = "0.20-0.30"
            else: b = ">=0.30"
            by_dis[b].append(p)
        bucket_report("By |fair_p - market| (only post-disagreement-filter signals)",
                      by_dis)

    # Uniqueness check: rounds with multiple signals
    by_round_start = defaultdict(list)
    for p in decided:
        try:
            rs = int(p["slug"].split("-")[-1])
            by_round_start[rs].append(p)
        except (ValueError, IndexError):
            pass
    multi = {ts: ps for ts, ps in by_round_start.items() if len(ps) > 1}
    if multi:
        print(f"\n== Rounds with multiple picks (correlation risk) ==")
        print(f"  {len(multi)} rounds had 2+ signals. In these:")
        correlated_same = 0
        for ts, ps in multi.items():
            outcomes = set(p["result"] for p in ps)
            if len(outcomes) == 1:
                correlated_same += 1
        print(f"  {correlated_same}/{len(multi)} rounds had ALL picks win OR all lose (correlation).")
        print(f"  Implication: treat each round as ~1 sample, not len(picks).")

    # Biggest winners + biggest losers
    print("\n== Top 5 biggest P&L picks ==")
    for p in sorted(decided, key=lambda x: -abs(x["pl"]))[:5]:
        print(f"  {p['result']:4}  ${p['pl']:+7.2f}  {p['asset']} {p['side']:3}  "
              f"ask={p['ask']:.3f}  fair_p={p['fair_p']:.2f}  "
              f"edge={p['edge']*100:4.1f}%  {p['slug']}")


if __name__ == "__main__":
    main()
