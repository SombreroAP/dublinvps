"""Backtest paper trades. Handles both log formats:
  - LEGACY: single "paper.signal" row per pick, resolved via Gamma lookup
    (binary win/loss at $1 payout).
  - ACTIVE: "entry" + matching "exit_<kind>" row pair, net P&L already
    computed in the exit row.
"""
import json, subprocess
from datetime import datetime, timezone

CHAINLINK_CUTOFF = 1776250620  # systemd go-live

def fetch(slug):
    try:
        out = subprocess.check_output([
            "curl", "-s", "-H", "User-Agent: Mozilla/5.0",
            f"https://gamma-api.polymarket.com/events?slug={slug}"
        ], timeout=8)
        d = json.loads(out)
        if not d: return "notfound"
        m = d[0].get("markets", [{}])[0]
        if not m.get("closed"): return "open"
        op = json.loads(m.get("outcomePrices") or "[]")
        if op == ["1", "0"]: return "UP"
        if op == ["0", "1"]: return "DOWN"
        return f"unknown({op})"
    except Exception as e:
        return f"err"


def _fmt(dt):
    return dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M:%S")


rows = [json.loads(l) for l in open("/opt/sniper/paper_trades.jsonl")]
post = [r for r in rows if r["ts"] >= CHAINLINK_CUTOFF]

# Split by format.
active_entries = [r for r in post if r.get("event") == "entry"]
active_exits = [r for r in post if str(r.get("event", "")).startswith("exit_")]
legacy_signals = [r for r in post if "event" not in r]

# ---- Legacy: dedup best-edge per (slug, side), lookup resolution ----
best_legacy = {}
for r in legacy_signals:
    k = (r["slug"], r["side"])
    if k not in best_legacy or r["edge"] > best_legacy[k]["edge"]:
        best_legacy[k] = r

# ---- Active: pair entries with exits by position_id ----
exits_by_pid = {r["position_id"]: r for r in active_exits}

tot = 0.0
wins = losses = pending = 0
rows_fmt = []  # (sort_ts, display_line)

# Process legacy signals
for (slug, side), r in best_legacy.items():
    result = fetch(slug)
    dt = datetime.fromtimestamp(r["ts"], tz=timezone.utc)
    date_str, time_str = _fmt(dt)
    if result in ("UP", "DOWN"):
        won = (side=="YES" and result=="UP") or (side=="NO" and result=="DOWN")
        shares = r["size_usdc"] / r["ask"]
        pl = shares * (1.0 if won else 0.0) - r["size_usdc"] - r["size_usdc"] * r["fee"]
        status = "WIN" if won else "LOSS"
        if won: wins += 1
        else: losses += 1
    else:
        pending += 1; pl = 0.0; status = result
    tot += pl
    rows_fmt.append((r["ts"], f"{date_str:12} {time_str:8} [legacy  ] {r['asset']:5} {side:4} "
                             f"ask={r['ask']:>5.2f} fair_p={r['fair_p']:>5.2f} "
                             f"edge={r['edge']:>5.2%} {status:8} pl={pl:>+7.2f}  {slug}"))

# Process active (entry + exit) positions
for entry in active_entries:
    pid = entry["position_id"]
    ex = exits_by_pid.get(pid)
    dt = datetime.fromtimestamp(entry["ts"], tz=timezone.utc)
    date_str, time_str = _fmt(dt)
    if ex is None:
        pending += 1
        status = "OPEN"
        pl = 0.0
        kind = "open"
        hold = None
    else:
        kind = ex["event"].split("_", 1)[1]  # tp / sl / resolve / expired
        pl = ex["net_pl_usdc"]
        hold = ex.get("hold_sec")
        if pl > 0:
            wins += 1
            status = f"WIN({kind})"
        elif pl < 0:
            losses += 1
            status = f"LOSS({kind})"
        else:
            status = f"BREAK({kind})"
    tot += pl
    hold_s = f"{hold:>4.0f}s" if hold is not None else "  --"
    rows_fmt.append((entry["ts"],
        f"{date_str:12} {time_str:8} [active  ] {entry['asset']:5} {entry['side']:4} "
        f"ask={entry['ask']:>5.2f} fair_p={entry['fair_p']:>5.2f} "
        f"edge={entry['edge']:>5.2%} {status:11} pl={pl:>+7.2f}  hold={hold_s}  {entry['slug']}"))

print(f"{'Date':12} {'Time':8} {'Format':10} {'Asset':5} {'Side':4}  stats…")
for _, line in sorted(rows_fmt, key=lambda x: x[0]):
    print(line)

total = wins + losses + pending
print(f"\nunique trades: {total}  wins: {wins}  losses: {losses}  pending/open: {pending}")
if wins + losses:
    print(f"win rate: {wins/(wins+losses):.1%}")
print(f"total P&L: ${tot:+.2f}")

# Breakdown of active-only stats (so we can see how active strategy is doing)
active_closed = [r for r in active_exits]
if active_closed:
    pl_list = [r["net_pl_usdc"] for r in active_closed]
    w = [p for p in pl_list if p > 0]
    l = [p for p in pl_list if p < 0]
    print(f"\n--- ACTIVE STRATEGY ONLY (entry+exit pairs) ---")
    print(f"closed: {len(active_closed)}  wins: {len(w)}  losses: {len(l)}")
    if w: print(f"avg win:  ${sum(w)/len(w):+.2f}")
    if l: print(f"avg loss: ${sum(l)/len(l):+.2f}")
    print(f"net P&L:  ${sum(pl_list):+.2f}")
    # Breakdown by exit kind
    from collections import Counter
    kinds = Counter(r["event"] for r in active_closed)
    print("by exit kind:", dict(kinds))
