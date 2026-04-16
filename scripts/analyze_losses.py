"""Break down sniper wins vs losses by feature buckets to find patterns.

Reads /opt/sniper/paper_trades.jsonl, matches against Polymarket resolutions,
then bins by (sec_left, ask, fair_p, edge, asset, side) to find where we lose.
"""
from __future__ import annotations

import json
import statistics as s
import subprocess
from collections import defaultdict
from pathlib import Path

LOG = Path("/opt/sniper/paper_trades.jsonl")
# Only consider signals fired AFTER the strict filters were deployed.
# (dust-ask / partial-fill / >30% edge filters landed in commit f0d30b5)
CUTOFF = 1776270000  # adjust if needed


def fetch_outcome(slug: str) -> str | None:
    try:
        raw = subprocess.check_output([
            "curl", "-sS", "-H", "User-Agent: Mozilla/5.0",
            f"https://gamma-api.polymarket.com/events?slug={slug}",
        ], timeout=6)
        d = json.loads(raw)
        if not d:
            return None
        m = d[0]["markets"][0]
        op_raw = m.get("outcomePrices") or "[]"
        op = [float(x) for x in json.loads(op_raw)]
        if len(op) < 2:
            return None
        if op[0] >= 0.95:
            return "UP"
        if op[1] >= 0.95:
            return "DOWN"
    except Exception:
        return None
    return None


def main() -> None:
    rows = [json.loads(l) for l in LOG.open()]
    post = [r for r in rows if r["ts"] >= CUTOFF]
    print(f"Rows: total {len(rows)}  post-cutoff {len(post)}")
    if not post:
        print("No rows after cutoff, adjust CUTOFF constant")
        return

    # Dedup by (slug, side) — keep highest-edge entry
    best: dict[tuple[str, str], dict] = {}
    for r in post:
        k = (r["slug"], r["side"])
        if k not in best or r.get("edge", 0) > best[k].get("edge", 0):
            best[k] = r
    print(f"Unique (slug,side) picks: {len(best)}\n")

    # Fetch outcomes
    resolved = []
    pending = 0
    for (slug, side), r in best.items():
        outcome = fetch_outcome(slug)
        if outcome is None:
            pending += 1
            continue
        won = (side == "YES" and outcome == "UP") or \
              (side == "NO" and outcome == "DOWN")
        r["_won"] = won
        r["_outcome"] = outcome
        resolved.append(r)
    print(f"Resolved: {len(resolved)}  Pending: {pending}")
    if not resolved:
        return

    wins = [r for r in resolved if r["_won"]]
    losses = [r for r in resolved if not r["_won"]]
    print(f"Wins: {len(wins)}  Losses: {len(losses)}  "
          f"Win rate: {100*len(wins)/len(resolved):.1f}%")

    # P&L (realistic: using fees already logged)
    total_pl = 0.0
    for r in resolved:
        shares = r["size_usdc"] / r["ask"] if r["ask"] > 0 else 0
        payoff = shares * (1.0 if r["_won"] else 0.0)
        pl = payoff - r["size_usdc"] - r["size_usdc"] * r.get("fee", 0)
        r["_pl"] = pl
        total_pl += pl
    print(f"Total P&L: ${total_pl:+.2f}\n")

    def bucket_stats(label: str, key_fn, buckets):
        print(f"=== {label} ===")
        by_b: dict = defaultdict(lambda: {"n": 0, "wins": 0, "pl": 0.0})
        for r in resolved:
            v = key_fn(r)
            # find bucket
            for lo, hi, name in buckets:
                if lo <= v < hi:
                    by_b[name]["n"] += 1
                    if r["_won"]:
                        by_b[name]["wins"] += 1
                    by_b[name]["pl"] += r["_pl"]
                    break
        print(f"  {'bucket':<18} {'n':>4} {'win%':>6} {'avg_pl':>9} {'total_pl':>10}")
        for _, _, name in buckets:
            b = by_b[name]
            if b["n"] == 0:
                continue
            wr = 100 * b["wins"] / b["n"]
            avg = b["pl"] / b["n"]
            print(f"  {name:<18} {b['n']:>4} {wr:>5.1f}% {avg:>+8.2f}  {b['pl']:>+10.2f}")
        print()

    bucket_stats("By sec_left at signal (how early we fire)",
        lambda r: r.get("sec_left", 0),
        [(0, 5, "T-5s  or less"), (5, 10, "T-5 to T-10"),
         (10, 20, "T-10 to T-20"), (20, 30, "T-20 to T-30"),
         (30, 45, "T-30 to T-45"), (45, 1e9, "T-45+")])

    bucket_stats("By ask price",
        lambda r: r.get("ask", 0),
        [(0, 0.10, "ask < 0.10"), (0.10, 0.30, "0.10-0.30"),
         (0.30, 0.50, "0.30-0.50"), (0.50, 0.70, "0.50-0.70"),
         (0.70, 0.85, "0.70-0.85"), (0.85, 0.95, "0.85-0.95"),
         (0.95, 1.01, "0.95+")])

    bucket_stats("By fair_p",
        lambda r: r.get("fair_p", 0),
        [(0, 0.60, "<0.60"), (0.60, 0.75, "0.60-0.75"),
         (0.75, 0.85, "0.75-0.85"), (0.85, 0.92, "0.85-0.92"),
         (0.92, 0.97, "0.92-0.97"), (0.97, 1.01, "0.97+")])

    bucket_stats("By edge",
        lambda r: r.get("edge", 0),
        [(0, 0.03, "<3%"), (0.03, 0.05, "3-5%"), (0.05, 0.08, "5-8%"),
         (0.08, 0.12, "8-12%"), (0.12, 0.18, "12-18%"), (0.18, 1, "18%+")])

    bucket_stats("By asset",
        lambda r: hash(r.get("asset", "?")),  # hack — we'll just iterate
        [])  # skip generic bucketing

    print("=== By asset ===")
    by_a: dict = defaultdict(lambda: {"n": 0, "wins": 0, "pl": 0.0})
    for r in resolved:
        a = r.get("asset", "?")
        by_a[a]["n"] += 1
        if r["_won"]:
            by_a[a]["wins"] += 1
        by_a[a]["pl"] += r["_pl"]
    print(f"  {'asset':<8} {'n':>4} {'win%':>6} {'total_pl':>10}")
    for a, b in sorted(by_a.items(), key=lambda x: -x[1]["pl"]):
        wr = 100 * b["wins"] / b["n"] if b["n"] else 0
        print(f"  {a:<8} {b['n']:>4} {wr:>5.1f}%  {b['pl']:>+10.2f}")
    print()

    # Worst losses — show full detail
    losses.sort(key=lambda r: r["_pl"])
    print(f"=== Worst 10 losses ===")
    for r in losses[:10]:
        print(f"  {r['asset']:3s} {r['side']:3s}  T-{r.get('sec_left', 0):4.1f}s  "
              f"ask={r['ask']:.3f}  fair_p={r.get('fair_p', 0):.2f}  "
              f"edge={r.get('edge', 0)*100:5.2f}%  "
              f"opening={r.get('opening', 0):.2f}  current={r.get('current', 0):.2f}  "
              f"outcome={r['_outcome']}  pl=${r['_pl']:+.2f}")


if __name__ == "__main__":
    main()
