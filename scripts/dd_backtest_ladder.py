"""Historical backtest: simulate our both-sides ladder on every round
Dimpled-Dill played, using actual CLOB trade history for fills.

For each unique round in dd_rounds.jsonl:
  1. Fetch resolution (UP/DOWN)
  2. Fetch full trade history for the conditionId
  3. For each side (YES, NO):
     - Simulate ladder fills using the same model as the dashboard
     - Compute realistic P&L
  4. Aggregate

Output: per-round P&L, summary stats, per-rung fill rates.
"""
from __future__ import annotations

import json
import subprocess
import time
from collections import defaultdict
from pathlib import Path

DATA = Path("/opt/sniper/data")
ROUNDS = DATA / "dd_rounds.jsonl"
OUT = DATA / "dd_ladder_backtest.jsonl"

LADDER = [(0.03, 1.0), (0.02, 2.0), (0.01, 5.0)]  # match the bot
TIME_WINDOW_AFTER_END = 600  # seconds — fills can come well after round_end


def fetch_market_info(slug: str) -> dict:
    """Returns {condition_id, yes_token, no_token, outcome, trades}."""
    out = {"condition_id": "", "yes_token": "", "no_token": "",
           "outcome": None, "trades": []}
    try:
        raw = subprocess.check_output([
            "curl", "-s", "-H", "User-Agent: Mozilla/5.0",
            f"https://gamma-api.polymarket.com/events?slug={slug}",
        ], timeout=8)
        d = json.loads(raw)
        if not d:
            return out
        m = d[0]["markets"][0]
        tokens = json.loads(m.get("clobTokenIds") or "[]")
        if len(tokens) != 2:
            return out
        out["condition_id"] = m["conditionId"]
        out["yes_token"] = str(tokens[0])
        out["no_token"] = str(tokens[1])

        # Resolution
        op_raw = m.get("outcomePrices") or "[]"
        try:
            op = [float(x) for x in json.loads(op_raw)]
        except (ValueError, TypeError):
            op = []
        if len(op) == 2:
            if op[0] >= 0.95:
                out["outcome"] = "UP"
            elif op[1] >= 0.95:
                out["outcome"] = "DOWN"

        # Trades — paginate deeply
        all_trades = []
        for offset in range(0, 5000, 500):
            try:
                raw = subprocess.check_output([
                    "curl", "-s", "-H", "User-Agent: Mozilla/5.0",
                    f"https://data-api.polymarket.com/trades"
                    f"?market={out['condition_id']}&limit=500&offset={offset}",
                ], timeout=10)
                page = json.loads(raw)
                if not isinstance(page, list) or not page:
                    break
                all_trades.extend(page)
                if len(page) < 500:
                    break
            except Exception:
                break
        out["trades"] = all_trades
    except Exception:
        pass
    return out


def simulate_fills(rungs, our_token, trades, round_start, round_end):
    in_window = [
        (float(t["price"]), float(t["size"]))
        for t in trades
        if str(t.get("asset", "")) == our_token
        and round_start - 5 <= int(t.get("timestamp", 0))
                              <= round_end + TIME_WINDOW_AFTER_END
    ]
    breakdown = []
    total_shares = 0.0
    for rung_price, usdc in rungs:
        my_shares = usdc / rung_price
        vol_at_or_below = sum(sz for px, sz in in_window if px <= rung_price + 1e-9)
        filled = vol_at_or_below >= my_shares
        breakdown.append({"price": rung_price, "usdc": usdc,
                          "my_shares": my_shares, "filled": filled})
        if filled:
            total_shares += my_shares
    return total_shares, breakdown


def main() -> None:
    if not ROUNDS.exists():
        print(f"Run dd_aggregate.py first ({ROUNDS} missing).")
        return
    rounds = [json.loads(l) for l in ROUNDS.open()]
    # Dedupe by slug — DD often had both-side picks; we want unique markets
    slugs = sorted({r["slug"] for r in rounds},
                   key=lambda s: int(s.split("-")[-1]))
    print(f"Unique rounds: {len(slugs)}")
    print()

    # Pre-fetch resolution for all rounds (fast: just one Gamma call each).
    # Aggregate as we go.
    per_round = []
    n_resolved = 0
    n_filled_any = 0
    rung_fill_counts = defaultdict(int)
    rung_total = defaultdict(int)
    pl_total = 0.0
    stake_total = 0.0
    pl_per_asset = defaultdict(float)
    stake_per_asset = defaultdict(float)
    win_count = defaultdict(int)
    win_loss_count = defaultdict(int)

    for i, slug in enumerate(slugs):
        info = fetch_market_info(slug)
        round_start = int(slug.split("-")[-1])
        round_end = round_start + 300
        asset = slug.split("-")[0].upper()
        outcome = info["outcome"]
        if outcome is None:
            per_round.append({"slug": slug, "outcome": None, "skipped": True})
            continue
        n_resolved += 1
        any_filled_this_round = False

        for side_label in ("YES", "NO"):
            our_token = info["yes_token"] if side_label == "YES" else info["no_token"]
            if not our_token or not info["trades"]:
                continue
            shares, breakdown = simulate_fills(
                LADDER, our_token, info["trades"], round_start, round_end,
            )
            real_stake = sum(b["usdc"] for b in breakdown if b["filled"])
            won = (side_label == "YES" and outcome == "UP") or \
                  (side_label == "NO" and outcome == "DOWN")
            payoff = shares * (1.0 if won else 0.0)
            real_pl = payoff - real_stake

            for b in breakdown:
                rung_total[b["price"]] += 1
                if b["filled"]:
                    rung_fill_counts[b["price"]] += 1
                    any_filled_this_round = True

            stake_total += real_stake
            pl_total += real_pl
            pl_per_asset[asset] += real_pl
            stake_per_asset[asset] += real_stake
            if real_stake > 0:
                win_loss_count[asset] += 1
                if real_pl > 0:
                    win_count[asset] += 1

            per_round.append({
                "slug": slug, "side": side_label, "outcome": outcome,
                "won": won, "real_stake": real_stake, "real_pl": real_pl,
                "fillable_rungs": sum(1 for b in breakdown if b["filled"]),
            })

        if any_filled_this_round:
            n_filled_any += 1
        if (i + 1) % 25 == 0:
            print(f"  processed {i+1}/{len(slugs)}  running PL: ${pl_total:+.2f}  staked: ${stake_total:.2f}")

    with OUT.open("w") as f:
        for r in per_round:
            f.write(json.dumps(r) + "\n")

    print()
    print("=" * 70)
    print(" HISTORICAL BACKTEST: BOTH-SIDES LADDER ON DD's ROUNDS")
    print("=" * 70)
    print(f"Total unique rounds:    {len(slugs)}")
    print(f"Resolved rounds:        {n_resolved}")
    print(f"Rounds with any fill:   {n_filled_any} ({100*n_filled_any/max(1,n_resolved):.1f}%)")
    print()
    print(f"Total stake (rungs that filled): ${stake_total:.2f}")
    print(f"Total realistic P&L:             ${pl_total:+.2f}")
    if stake_total > 0:
        print(f"ROI on staked capital:           {100*pl_total/stake_total:+.1f}%")
    print()
    print("Per-rung fill rate:")
    for price in (0.03, 0.02, 0.01):
        n = rung_total[price]
        f = rung_fill_counts[price]
        rate = (100 * f / n) if n else 0
        print(f"  ${price:.2f}: {f}/{n} ({rate:.1f}%)")
    print()
    print("Per-asset:")
    for a in sorted(pl_per_asset):
        wlc = win_loss_count[a]
        wc = win_count[a]
        wr = (100 * wc / wlc) if wlc else 0
        print(f"  {a:5s}: stake ${stake_per_asset[a]:7.2f}  PL ${pl_per_asset[a]:+8.2f}  fillable rounds {wlc:3d}  wins {wc:3d} ({wr:.1f}%)")
    print()
    print(f"Per-round detail saved to {OUT}")


if __name__ == "__main__":
    main()
