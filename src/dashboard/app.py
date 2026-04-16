"""Dashboard for the Polymarket sniper bot.

- Reads paper_trades.jsonl (no DB, no shared memory with bot).
- Queries Gamma for upcoming markets + resolution status.
- Queries Polymarket RTDS Chainlink feed for live prices (own WS connection).
- HTTP Basic Auth — password from DASHBOARD_PASSWORD env var.

Run: /opt/sniper/.venv/bin/python -m src.dashboard.app
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import subprocess
import time
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from src.config import settings
from src.feeds.chainlink import ChainlinkFeed
from src.polymarket.gamma import fetch_active_markets

PAPER_LOG = Path("/opt/sniper/paper_trades.jsonl")
CHAINLINK_CUTOFF = 1776250620  # systemd go-live; edit as needed

DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD") or ""

app = FastAPI(title="Sniper Dashboard")
security = HTTPBasic()
_chainlink = ChainlinkFeed()
_started = time.time()
_resolution_cache: dict[str, tuple[float, str]] = {}  # slug -> (fetched_at, result)
_backtest_cache: tuple[float, dict] | None = None
# Live ask cache: (slug, side) -> (fetched_at, ask). ttl 2s.
_live_ask_cache: dict[tuple[str, str], tuple[float, float | None]] = {}


def _check_auth(creds: HTTPBasicCredentials = Depends(security)) -> None:
    if not DASHBOARD_PASSWORD:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "DASHBOARD_PASSWORD not set")
    ok = (creds.username == "admin" and
          secrets.compare_digest(creds.password, DASHBOARD_PASSWORD))
    if not ok:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED,
                            headers={"WWW-Authenticate": "Basic"})


@app.on_event("startup")
async def _startup() -> None:
    asyncio.create_task(_chainlink.run())


def _read_signals() -> list[dict]:
    if not PAPER_LOG.exists():
        return []
    out = []
    with PAPER_LOG.open() as f:
        for line in f:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _fetch_live_ask(slug: str, side: str) -> float | None:
    """Return current CLOB best-ask for (slug, side). ttl 2s."""
    key = (slug, side)
    now = time.time()
    hit = _live_ask_cache.get(key)
    if hit and now - hit[0] < 1.0:
        return hit[1]
    try:
        raw = subprocess.check_output([
            "curl", "-s", "-H", "User-Agent: Mozilla/5.0",
            f"https://gamma-api.polymarket.com/events?slug={slug}",
        ], timeout=5)
        d = json.loads(raw)
        if not d:
            _live_ask_cache[key] = (now, None)
            return None
        m = d[0].get("markets", [{}])[0]
        tokens = json.loads(m.get("clobTokenIds") or "[]")
        if len(tokens) != 2:
            _live_ask_cache[key] = (now, None)
            return None
        tid = tokens[0] if side == "YES" else tokens[1]
        raw = subprocess.check_output([
            "curl", "-s",
            f"https://clob.polymarket.com/book?token_id={tid}",
        ], timeout=4)
        b = json.loads(raw)
        asks = b.get("asks") or []
        best_ask = min((float(x["price"]) for x in asks), default=None)
    except Exception:
        best_ask = None
    _live_ask_cache[key] = (now, best_ask)
    return best_ask


def _fetch_resolution(slug: str) -> str:
    """Cached Gamma resolution lookup.
    UP/DOWN once resolved — cached FOREVER (they don't change).
    open/notfound — cached 30s (might flip soon).
    Returns UP/DOWN if resolved (or implicitly decided post-end with
    outcomePrices >= 0.95), else open/notfound/err."""
    now = time.time()
    hit = _resolution_cache.get(slug)
    if hit:
        age = now - hit[0]
        # Terminal states stay cached forever. Non-terminal refetch after 30s.
        if hit[1] in ("UP", "DOWN") or age < 30:
            return hit[1]
    try:
        raw = subprocess.check_output([
            "curl", "-s", "-H", "User-Agent: Mozilla/5.0",
            f"https://gamma-api.polymarket.com/events?slug={slug}",
        ], timeout=6)
        d = json.loads(raw)
        if not d:
            result = "notfound"
        else:
            m = d[0].get("markets", [{}])[0]
            op_raw = m.get("outcomePrices") or "[]"
            try:
                op = [float(x) for x in json.loads(op_raw)]
            except (ValueError, TypeError):
                op = []
            # Hard-resolved: outcomePrices == ["1","0"] / ["0","1"] AND closed.
            # Soft-resolved: market past endDate AND outcomePrices >= 0.95
            #   (Polymarket has ~30-60s lag between round end and writing the
            #    official close, but the orderbook converges immediately.)
            from datetime import datetime, timezone
            past_end = False
            if m.get("endDate"):
                try:
                    end_ts = datetime.fromisoformat(
                        m["endDate"].replace("Z", "+00:00")).timestamp()
                    past_end = time.time() > end_ts
                except (ValueError, TypeError):
                    pass
            if len(op) == 2 and (m.get("closed") or past_end):
                if op[0] >= 0.95:
                    result = "UP"
                elif op[1] >= 0.95:
                    result = "DOWN"
                elif m.get("closed"):
                    result = "unknown"  # closed but ambiguous prices
                else:
                    result = "open"  # past end but not yet decided
            else:
                result = "open"
    except Exception:
        result = "err"
    _resolution_cache[slug] = (now, result)
    return result


def _compute_backtest() -> dict:
    global _backtest_cache
    if _backtest_cache and time.time() - _backtest_cache[0] < 3:
        return _backtest_cache[1]

    rows = _read_signals()
    post = [r for r in rows if r["ts"] >= CHAINLINK_CUTOFF]
    best: dict = {}
    for r in post:
        k = (r["slug"], r["side"])
        if k not in best or r["edge"] > best[k]["edge"]:
            best[k] = r

    picks = []
    tot_pl = 0.0
    wins = losses = pending = 0
    for (slug, side), r in sorted(best.items(), key=lambda kv: kv[1]["ts"], reverse=True):
        res = _fetch_resolution(slug)
        if res in ("UP", "DOWN"):
            won = (side == "YES" and res == "UP") or (side == "NO" and res == "DOWN")
            shares = r["size_usdc"] / r["ask"]
            pl = shares * (1.0 if won else 0.0) - r["size_usdc"] - r["size_usdc"] * r["fee"]
            status_ = "WIN" if won else "LOSS"
            if won:
                wins += 1
            else:
                losses += 1
        else:
            pl = 0.0
            status_ = res
            pending += 1
        tot_pl += pl
        target = "UP" if side == "YES" else "DOWN"
        open_px = r.get("opening")
        # Phase: active (round running) / pending (round over, awaiting
        # Polymarket resolution) / WIN / LOSS / etc. Determines whether we
        # show a moving "live px" or freeze at the round-close snapshot.
        phase = status_
        round_end_ts = None
        if status_ == "open":
            try:
                round_end_ts = int(slug.split("-")[-1]) + 300
                phase = "active" if round_end_ts > time.time() else "pending"
            except (ValueError, IndexError):
                phase = "active"
        # For ACTIVE picks, "live" is the current moving Chainlink price.
        # For PENDING / resolved picks, freeze at the Chainlink close snapshot.
        if phase == "active":
            live = _chainlink.last_price.get(r["asset"])
        else:
            live = None
            if round_end_ts is not None:
                live = _chainlink.opening_at(r["asset"], round_end_ts)
            if live is None:
                # Fallback when round_end is outside our rolling Chainlink
                # history (dashboard restarted after the round ended).
                live = _chainlink.last_price.get(r["asset"])
        delta = (live - open_px) if (live is not None and open_px is not None) else None
        live_ask = _fetch_live_ask(slug, side) if phase == "active" else None
        if delta is None:
            trending = None
        else:
            trending = (target == "UP" and delta > 0) or (target == "DOWN" and delta < 0)

        # Expected result / P&L projection for PENDING picks using the
        # frozen Chainlink close. Polymarket resolves UP on close ≥ open.
        expected_result = None
        expected_pl = None
        if phase == "pending" and live is not None and open_px is not None:
            close_up = live >= open_px
            expected_won = (target == "UP" and close_up) or \
                           (target == "DOWN" and not close_up)
            expected_result = "WIN" if expected_won else "LOSS"
            shares = r["size_usdc"] / r["ask"] if r["ask"] > 0 else 0
            expected_pl = (shares * (1.0 if expected_won else 0.0)
                           - r["size_usdc"] - r["size_usdc"] * r["fee"])

        picks.append({
            "ts": r["ts"], "slug": slug, "asset": r["asset"], "side": side,
            "target": target,
            "ask": r["ask"], "fair_p": r["fair_p"], "edge": r["edge"],
            "fee": r["fee"], "size_usdc": r["size_usdc"],
            "opening": open_px, "live": live, "delta": delta,
            "live_ask": live_ask, "trending": trending, "phase": phase,
            "expected_result": expected_result, "expected_pl": expected_pl,
            "result": status_, "pl": pl,
        })

    result = {
        "total_signals": len(rows),
        "post_chainlink_signals": len(post),
        "unique_picks": len(best),
        "wins": wins, "losses": losses, "pending": pending,
        "win_rate": (wins / (wins + losses)) if (wins + losses) else None,
        "total_pl": tot_pl,
        "picks": picks[:50],
    }
    _backtest_cache = (time.time(), result)
    return result


_live_state_cache: tuple[float, list] | None = None
# Cache of (slug, side_label) -> list of (price, size, ts) trades on the winning token.
_trades_cache: dict[str, tuple[float, dict]] = {}


def _fetch_market_trades(slug: str) -> dict:
    """Fetch trades for a 5m market. Returns:
        {'condition_id': str, 'yes_token': str, 'no_token': str, 'trades': [...]}
    where trades is a list of {price, size, timestamp, asset} dicts.
    Cached 5min per slug (trades for resolved markets don't change).
    """
    now = time.time()
    hit = _trades_cache.get(slug)
    if hit and now - hit[0] < 300:
        return hit[1]

    out: dict = {"condition_id": "", "yes_token": "", "no_token": "", "trades": []}
    try:
        raw = subprocess.check_output([
            "curl", "-s", "-H", "User-Agent: Mozilla/5.0",
            f"https://gamma-api.polymarket.com/events?slug={slug}",
        ], timeout=6)
        d = json.loads(raw)
        if not d:
            _trades_cache[slug] = (now, out)
            return out
        m = d[0]["markets"][0]
        tokens = json.loads(m.get("clobTokenIds") or "[]")
        if len(tokens) != 2:
            _trades_cache[slug] = (now, out)
            return out
        out["condition_id"] = m["conditionId"]
        out["yes_token"] = str(tokens[0])
        out["no_token"] = str(tokens[1])

        # Fetch trades for this conditionId. Paginate by offset.
        all_trades: list[dict] = []
        for offset in (0, 500, 1000, 1500, 2000, 2500):
            try:
                raw = subprocess.check_output([
                    "curl", "-s", "-H", "User-Agent: Mozilla/5.0",
                    f"https://data-api.polymarket.com/trades"
                    f"?market={out['condition_id']}&limit=500&offset={offset}",
                ], timeout=8)
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
    _trades_cache[slug] = (now, out)
    return out


def _simulate_fills(rungs: list, winning_token: str, trades: list[dict],
                    round_start: int, round_end: int) -> tuple[float, list]:
    """For each ladder rung at price P, check if our maker bid would have been
    filled by inspecting actual trades on the winning token within the round
    window.

    Conservative model: rung fills at posted price IF total trade volume on the
    winning token at price <= P during the round window >= our share size.
    (Approximates queue priority: if someone got filled at our price level
    AND there was enough volume, our order was probably in the queue too.)

    Returns (total_winning_shares, [{price, usdc, filled, fill_reason}, ...]).
    """
    # Window: from order-placement (round start) through the post-round
    # convergence trading. Polymarket markets keep trading after round_end
    # while the orderbook converges to the resolved $1/$0 prices — that's
    # where the deepest fills happen for ladder strategies.
    in_window = [
        (float(t["price"]), float(t["size"]), int(t["timestamp"]))
        for t in trades
        if str(t.get("asset", "")) == winning_token
        and round_start - 5 <= int(t.get("timestamp", 0)) <= round_end + 600
    ]
    breakdown = []
    total_shares = 0.0
    for rung_price, usdc in rungs:
        my_shares = usdc / rung_price if rung_price > 0 else 0
        # Volume traded at-or-below our bid price during the round window
        vol_at_or_below = sum(sz for px, sz, _ in in_window if px <= rung_price + 1e-9)
        filled = vol_at_or_below >= my_shares
        breakdown.append({
            "price": rung_price, "usdc": usdc, "my_shares": my_shares,
            "traded_vol_at_or_below": vol_at_or_below,
            "filled": filled,
        })
        if filled:
            total_shares += my_shares
    return total_shares, breakdown


def _fair_yes_p(asset: str, current: float, opening: float,
                seconds_left: float) -> float:
    """Mirror of strategy.sniper.fair_yes_probability (Brownian). Keep in sync."""
    from math import erf, sqrt
    sigma = {"BTC": settings.sigma_bps_btc, "ETH": settings.sigma_bps_eth,
             "SOL": settings.sigma_bps_sol}.get(asset, 1.2)
    if seconds_left <= 0:
        return 1.0 if current >= opening else 0.0
    move_bps = (current - opening) / opening * 10_000
    sd = sigma * sqrt(seconds_left)
    if sd < 1e-9:
        return 1.0 if move_bps >= 0 else 0.0
    z = move_bps / sd
    return 0.5 * (1.0 + erf(z / sqrt(2)))


def _taker_fee(rate: float, exponent: float, price: float) -> float:
    if rate <= 0:
        return 0.0
    shape = max(0.0, 1.0 - 4.0 * (price - 0.5) ** 2)
    return rate * (shape ** exponent)


def _compute_live_state() -> list[dict]:
    """Per-asset: nearest upcoming round + bot's current thinking."""
    global _live_state_cache
    if _live_state_cache and time.time() - _live_state_cache[0] < 1.0:
        return _live_state_cache[1]

    now = time.time()
    ENTRY_START, ENTRY_END = settings.entry_window_start_sec, settings.entry_window_end_sec
    out = []
    for asset_short, asset in [("btc", "BTC"), ("eth", "ETH"), ("sol", "SOL")]:
        # Pick the nearest round that hasn't ended yet.
        base = (int(now) // 300) * 300
        for offset in (0, 300, 600):
            round_start = base + offset
            round_end = round_start + 300
            if round_end > now:
                break
        slug = f"{asset_short}-updown-5m-{round_start}"
        sec_left = round_end - now
        cur = _chainlink.last_price.get(asset)
        opening = _chainlink.opening_at(asset, round_start)

        yes_ask = no_ask = None
        fee_rate = fee_exp = None
        try:
            raw = subprocess.check_output([
                "curl", "-s", "-H", "User-Agent: Mozilla/5.0",
                f"https://gamma-api.polymarket.com/events?slug={slug}",
            ], timeout=4)
            d = json.loads(raw)
            if d:
                m = d[0]["markets"][0]
                tokens = json.loads(m.get("clobTokenIds") or "[]")
                fee_sched = m.get("feeSchedule") or {}
                fee_rate = float(fee_sched.get("rate", 0.0))
                fee_exp = float(fee_sched.get("exponent", 1.0))
                if len(tokens) == 2:
                    for label, tid, target in [("yes", tokens[0], "yes_ask"),
                                                ("no", tokens[1], "no_ask")]:
                        try:
                            rb = subprocess.check_output([
                                "curl", "-s",
                                f"https://clob.polymarket.com/book?token_id={tid}",
                            ], timeout=3)
                            bk = json.loads(rb)
                            asks = bk.get("asks") or []
                            val = min((float(x["price"]) for x in asks), default=None)
                            if target == "yes_ask":
                                yes_ask = val
                            else:
                                no_ask = val
                        except Exception:
                            pass
        except Exception:
            pass

        # Compute fair_p + edges + decision
        fair_p = None
        best_side = None
        best_edge = None
        best_ask = None
        in_window = ENTRY_END < sec_left <= ENTRY_START
        if cur is not None and opening is not None:
            fair_p = _fair_yes_p(asset, cur, opening, sec_left)
            if fair_p > 0.5 and yes_ask is not None and fee_rate is not None:
                e = fair_p - yes_ask - _taker_fee(fee_rate, fee_exp, yes_ask)
                if best_edge is None or e > best_edge:
                    best_edge, best_side, best_ask = e, "YES", yes_ask
            if fair_p < 0.5 and no_ask is not None and fee_rate is not None:
                p_no = 1 - fair_p
                e = p_no - no_ask - _taker_fee(fee_rate, fee_exp, no_ask)
                if best_edge is None or e > best_edge:
                    best_edge, best_side, best_ask = e, "NO", no_ask

        # Decision string
        threshold = settings.edge_threshold
        if not in_window:
            def _mmss(s: float) -> str:
                s = max(0, int(s))
                return f"{s // 60}:{s % 60:02d}"
            if sec_left > ENTRY_START:
                state = f"waiting — window opens in {_mmss(sec_left - ENTRY_START)}"
            else:
                state = f"past entry window (T-{_mmss(sec_left)})"
            action = None
        elif best_edge is None or best_edge <= threshold:
            state = f"in window — no edge (best {((best_edge or 0)*100):+.1f}%)"
            action = None
        else:
            state = f"in window — would buy {best_side} @ {best_ask:.2f} (edge {best_edge*100:.1f}%)"
            action = {"side": best_side, "ask": best_ask, "edge": best_edge}

        delta = (cur - opening) if (cur is not None and opening is not None) else None
        move_bps = ((cur - opening) / opening * 10_000) if delta is not None else None

        out.append({
            "asset": asset, "slug": slug,
            "round_start": round_start, "round_end": round_end,
            "round_end_unix_ts": round_end,  # for client-side countdown
            "seconds_left": sec_left,
            "opening": opening, "current": cur, "delta": delta, "move_bps": move_bps,
            "yes_ask": yes_ask, "no_ask": no_ask,
            "fair_p_yes": fair_p,
            "best_side": best_side, "best_edge": best_edge, "best_ask": best_ask,
            "in_window": in_window, "state": state, "action": action,
            "threshold": threshold,
        })

    _live_state_cache = (time.time(), out)
    return out


def _bot_status() -> dict:
    try:
        out = subprocess.check_output(
            ["systemctl", "is-active", "sniper.service"], timeout=3, text=True
        ).strip()
    except Exception:
        out = "unknown"
    return {"sniper_service": out}


# Stale-cache fallbacks. If any one computation throws (usually a subprocess
# curl timing out / Cloudflare 429'ing), return the last successful value
# rather than propagating a 500 to the frontend — that used to produce
# "blank dashboard every few polls". Only replaced when a new success lands.
_last_good: dict = {}


def _safe_call(key: str, fn):
    try:
        v = fn()
        _last_good[key] = v
        return v
    except Exception as e:
        log.error("safe_call.fallback", key=key, error=str(e))
        return _last_good.get(key, {})


@app.get("/api/summary")
def api_summary(_=Depends(_check_auth)) -> JSONResponse:
    return JSONResponse({
        "now": int(time.time()),
        "dashboard_uptime_sec": int(time.time() - _started),
        "bot": _bot_status(),
        "chainlink": {
            "connected": bool(_chainlink.last_price),
            "prices": _chainlink.last_price,
            "last_update_ms": _chainlink.last_ts_ms,
        },
        "backtest": _safe_call("backtest", _compute_backtest),
        "live":     _safe_call("live",     _compute_live_state),
    })


@app.get("/healthz")
def healthz() -> JSONResponse:
    return JSONResponse({"ok": True})


INDEX_HTML = """
<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sniper Dashboard</title>
<style>
:root { color-scheme: dark; --bg:#0b0f14; --panel:#141a22; --fg:#e6edf3; --mut:#7d8590; --ok:#3fb950; --bad:#f85149; --warn:#d29922; --acc:#58a6ff; }
* { box-sizing: border-box; }
body { margin:0; font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; background:var(--bg); color:var(--fg); }
header { padding:14px 20px; border-bottom:1px solid #1f2730; display:flex; justify-content:space-between; align-items:center; }
h1 { margin:0; font-size:16px; font-weight:600; letter-spacing:.3px; }
.grid { display:grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap:14px; padding:16px; }
.card { background:var(--panel); border:1px solid #1f2730; border-radius:10px; padding:14px 16px; }
.card h2 { margin:0 0 8px; font-size:12px; letter-spacing:.8px; text-transform:uppercase; color:var(--mut); font-weight:600; }
.kv { display:flex; justify-content:space-between; padding:4px 0; border-bottom:1px dashed #1f2730; }
.kv:last-child { border:0; }
.kv .k { color:var(--mut); }
.kv .v { font-variant-numeric: tabular-nums; }
.big { font-size:28px; font-weight:700; font-variant-numeric: tabular-nums; }
.ok { color:var(--ok); } .bad { color:var(--bad); } .warn { color:var(--warn); } .acc { color:var(--acc); } .mut { color:var(--mut); }
table { width:100%; border-collapse:collapse; font-size:12px; font-variant-numeric: tabular-nums; }
th, td { padding:6px 8px; text-align:left; border-bottom:1px solid #1f2730; }
th { color:var(--mut); font-weight:500; text-transform:uppercase; font-size:10px; letter-spacing:.6px; }
tr:hover td { background:#192029; }
.pill { display:inline-block; padding:1px 6px; border-radius:10px; font-size:10px; letter-spacing:.4px; }
.pill.WIN { background:#0f3a1c; color:#3fb950; }
.pill.LOSS { background:#4a1619; color:#f85149; }
.pill.open, .pill.pending, .pill.notfound, .pill.err { background:#342806; color:#d29922; }
.foot { padding:10px 20px; color:var(--mut); font-size:11px; border-top:1px solid #1f2730; display:flex; justify-content:space-between; }
.wrap { max-width:1200px; margin:0 auto; }
</style></head><body>
<header><div class="wrap" style="width:100%; display:flex; justify-content:space-between; align-items:center;">
<h1>🎯 Sniper Dashboard <span id="status" class="pill" style="margin-left:6px"></span></h1>
<div class="mut" id="clock"></div>
</div></header>

<div class="wrap">
<div class="grid">
  <div class="card"><h2>P&amp;L (simulated)</h2><div id="pl" class="big">—</div><div id="plsub" class="mut"></div></div>
  <div class="card"><h2>Win rate</h2><div id="winrate" class="big">—</div><div id="wr_sub" class="mut"></div></div>
  <div class="card"><h2>Unique picks</h2><div id="picks" class="big">—</div><div id="picks_sub" class="mut"></div></div>
  <div class="card"><h2>Chainlink prices</h2>
    <div class="kv"><span class="k">BTC</span><span class="v" id="p_btc">—</span></div>
    <div class="kv"><span class="k">ETH</span><span class="v" id="p_eth">—</span></div>
    <div class="kv"><span class="k">SOL</span><span class="v" id="p_sol">—</span></div>
  </div>
</div>

<div class="grid" style="padding:0 16px 16px">
  <div class="card" id="live_BTC"><h2>BTC · next round</h2><div class="live_body mut">loading…</div></div>
  <div class="card" id="live_ETH"><h2>ETH · next round</h2><div class="live_body mut">loading…</div></div>
  <div class="card" id="live_SOL"><h2>SOL · next round</h2><div class="live_body mut">loading…</div></div>
</div>

<div class="card" style="margin:0 16px 16px; border-color:#2d4a2d">
  <h2 style="color:#3fb950">● Active round <span id="active_count" class="mut" style="font-weight:400"></span></h2>
  <table id="active_tbl"><thead>
    <tr><th>Time</th><th>Asset</th><th>Target</th><th>Ask</th><th>Live ask</th><th>Fair p</th><th>Edge</th><th>Size</th><th>Open px</th><th>Live px</th><th>Δ</th><th>Slug</th></tr>
  </thead><tbody></tbody></table>
  <div id="active_empty" class="mut" style="padding:12px 4px; display:none">No picks in the currently-active round.</div>
</div>

<div class="card" style="margin:0 16px 16px; border-color:#4a3d1a">
  <h2 style="color:#d29922">⏳ Waiting to resolve <span id="pending_count" class="mut" style="font-weight:400"></span></h2>
  <table id="pending_tbl"><thead>
    <tr><th>Time</th><th>Asset</th><th>Target</th><th>Ask</th><th>Fair p</th><th>Edge</th><th>Size</th><th>Open px</th><th>Close px</th><th>Expected</th><th>Est P&amp;L</th><th>Slug</th></tr>
  </thead><tbody></tbody></table>
  <div id="pending_empty" class="mut" style="padding:12px 4px; display:none">No picks awaiting resolution.</div>
</div>

<div class="card" style="margin:0 16px 16px">
  <h2>Recent resolved picks (sniper)</h2>
  <table id="picks_tbl"><thead>
    <tr><th>Time</th><th>Asset</th><th>Target</th><th>Ask</th><th>Fair p</th><th>Edge</th><th>Size</th><th>Open px</th><th>Result</th><th>P&amp;L</th><th>Slug</th></tr>
  </thead><tbody></tbody></table>
</div>
</div>

<div class="foot">
  <div>timer 200ms · poll 500ms · CLOB ~1s · dashboard uptime <span id="uptime">—</span></div>
  <div>bot: <span id="bot_status" class="mut">—</span></div>
</div>

<script>
function fmt(n, d=2) { return n==null ? "—" : n.toFixed(d); }
function dur(s) {
  const h = Math.floor(s/3600), m = Math.floor((s%3600)/60);
  return h>0 ? `${h}h${m}m` : `${m}m`;
}
function fmtPxJS(v, a) { return v==null ? "—" : (a==="SOL" ? v.toFixed(3) : v.toFixed(2)); }

// Re-renders the 3 per-asset cards using the latest server snapshot plus
// CLIENT-SIDE clock for the countdown, so the timer ticks smoothly at 200ms
// regardless of how fast the server poll is.
function renderAssetCards() {
  const snapshot = window._liveSnapshot || [];
  const entryStart = 45, entryEnd = 5;
  const now = Date.now() / 1000;
  for (const s of snapshot) {
    const el = document.getElementById("live_" + s.asset);
    if (!el) continue;
    const body = el.querySelector(".live_body");
    const secLeft = s.round_end_unix_ts ? Math.max(0, s.round_end_unix_ts - now) : s.seconds_left;
    const secInt = secLeft|0;
    const mmss = `${Math.floor(Math.max(0,secInt)/60)}:${String(Math.max(0,secInt)%60).padStart(2,"0")}`;
    const inWin = (entryEnd < secLeft && secLeft <= entryStart);
    const dirCls = s.move_bps==null ? "mut" : (s.move_bps>0 ? "ok" : s.move_bps<0 ? "bad" : "mut");
    const dirArrow = s.move_bps==null ? "—" : (s.move_bps>=0 ? "↑" : "↓");
    const moveStr = s.move_bps==null ? "—" : `${dirArrow} ${s.move_bps>=0?"+":""}${s.move_bps.toFixed(1)} bps`;
    // Color Live px vs Open px: green if higher, red if lower.
    let liveCls = "mut";
    if (s.current != null && s.opening != null) {
      if (s.current > s.opening) liveCls = "ok";
      else if (s.current < s.opening) liveCls = "bad";
    }
    const edgeStr = s.best_edge==null ? "—" :
      `<span class="${s.best_edge>s.threshold?"ok":"mut"}">${s.best_side} ${(s.best_edge*100).toFixed(1)}%</span>`;
    let stateCls = "mut";
    if (s.action) stateCls = "ok";
    else if (inWin) stateCls = "warn";
    body.innerHTML = `
      <div class="kv"><span class="k">Round ends in</span><span class="v ${inWin?"warn":""}">${mmss} ${inWin?"· IN WINDOW":""}</span></div>
      <div class="kv"><span class="k">Open px</span><span class="v">${fmtPxJS(s.opening, s.asset)}</span></div>
      <div class="kv"><span class="k">Live px</span><span class="v ${liveCls}">${fmtPxJS(s.current, s.asset)}</span></div>
      <div class="kv"><span class="k">Move</span><span class="v ${dirCls}">${moveStr}</span></div>
      <div class="kv"><span class="k">YES ask / NO ask</span><span class="v">${s.yes_ask==null?"—":s.yes_ask.toFixed(2)} / ${s.no_ask==null?"—":s.no_ask.toFixed(2)}</span></div>
      <div class="kv"><span class="k">Fair p (YES)</span><span class="v">${s.fair_p_yes==null?"—":s.fair_p_yes.toFixed(2)}</span></div>
      <div class="kv"><span class="k">Best edge</span><span class="v">${edgeStr}</span></div>
      <div style="margin-top:8px; padding:8px; background:#0f141a; border-radius:6px; font-size:12px" class="${stateCls}">${s.state}</div>
    `;
  }
}
setInterval(renderAssetCards, 200);
async function refresh() {
  try {
    const r = await fetch("/api/summary", { credentials: "include" });
    if (!r.ok) throw new Error(r.status);
    const d = await r.json();
    const bt = d.backtest;
    const pl = bt.total_pl;
    const plEl = document.getElementById("pl");
    plEl.textContent = (pl>=0?"+":"") + "$" + pl.toFixed(2);
    plEl.className = "big " + (pl>0?"ok":pl<0?"bad":"mut");
    document.getElementById("plsub").textContent = `over ${bt.wins+bt.losses} resolved trades`;
    document.getElementById("winrate").textContent = bt.win_rate==null ? "—" : (bt.win_rate*100).toFixed(1)+"%";
    document.getElementById("wr_sub").textContent = `${bt.wins}W / ${bt.losses}L / ${bt.pending}P`;
    document.getElementById("picks").textContent = bt.unique_picks;
    document.getElementById("picks_sub").textContent = `from ${bt.post_chainlink_signals} post-Chainlink signals`;

    const cl = d.chainlink.prices || {};
    document.getElementById("p_btc").textContent = cl.BTC ? "$"+cl.BTC.toFixed(2) : "—";
    document.getElementById("p_eth").textContent = cl.ETH ? "$"+cl.ETH.toFixed(2) : "—";
    document.getElementById("p_sol").textContent = cl.SOL ? "$"+cl.SOL.toFixed(3) : "—";

    const fmtPx = (v, a) => v==null ? "—" : (a==="SOL" ? v.toFixed(3) : v.toFixed(2));

    // Stash for the client-side ticker (renderAssetCards runs every 200ms).
    window._liveSnapshot = d.live || [];
    window._threshold = (d.live && d.live[0]) ? d.live[0].threshold : 0.025;
    renderAssetCards();

    const activeBody = document.querySelector("#active_tbl tbody");
    const pendingBody = document.querySelector("#pending_tbl tbody");
    const resBody = document.querySelector("#picks_tbl tbody");
    activeBody.innerHTML = ""; pendingBody.innerHTML = ""; resBody.innerHTML = "";
    const activePicks = bt.picks.filter(p => p.phase === "active");
    const pendingPicks = bt.picks.filter(p => p.phase === "pending");
    const resolvedPicks = bt.picks.filter(p => p.result !== "open");
    document.getElementById("active_count").textContent = activePicks.length ? `(${activePicks.length})` : "";
    document.getElementById("active_empty").style.display = activePicks.length ? "none" : "block";
    document.getElementById("pending_count").textContent = pendingPicks.length ? `(${pendingPicks.length})` : "";
    document.getElementById("pending_empty").style.display = pendingPicks.length ? "none" : "block";

    for (const p of activePicks) {
      const t = new Date(p.ts*1000).toLocaleTimeString();
      const tgtCls = p.target==="UP" ? "ok" : "bad";
      const tgtArrow = p.target==="UP" ? "↑" : "↓";
      let deltaCell = "—", deltaCls = "mut";
      if (p.delta != null) {
        const sign = p.delta>=0 ? "+" : "";
        const deltaFmt = p.asset==="SOL" ? p.delta.toFixed(3) : p.delta.toFixed(2);
        deltaCell = `${sign}${deltaFmt}`;
        deltaCls = p.trending ? "ok" : "bad";
      }
      let liveAskCell = "—", liveAskCls = "mut";
      if (p.live_ask != null) {
        liveAskCell = p.live_ask.toFixed(2);
        if (p.live_ask < p.ask) liveAskCls = "ok";
        else if (p.live_ask > p.ask) liveAskCls = "bad";
      }
      const tr = document.createElement("tr");
      tr.innerHTML = `<td class="mut">${t}</td><td>${p.asset}</td>
        <td class="${tgtCls}">${tgtArrow} ${p.target}</td>
        <td>${p.ask.toFixed(2)}</td>
        <td class="${liveAskCls}">${liveAskCell}</td>
        <td>${p.fair_p.toFixed(2)}</td>
        <td>${(p.edge*100).toFixed(1)}%</td>
        <td>$${p.size_usdc.toFixed(0)}</td>
        <td>${fmtPx(p.opening, p.asset)}</td>
        <td>${fmtPx(p.live, p.asset)}</td>
        <td class="${deltaCls}">${deltaCell}</td>
        <td class="mut">${p.slug}</td>`;
      activeBody.appendChild(tr);
    }

    // Pending resolution: round ended, just waiting on Polymarket to write the outcome.
    for (const p of pendingPicks) {
      const t = new Date(p.ts*1000).toLocaleTimeString();
      const tgtCls = p.target==="UP" ? "ok" : "bad";
      const tgtArrow = p.target==="UP" ? "↑" : "↓";
      // Expected result + P&L based on Chainlink close vs open (our best
      // projection until Polymarket officially writes the outcome).
      let expCell = "—", expCls = "mut";
      if (p.expected_result) {
        expCell = `<span class="pill ${p.expected_result}">${p.expected_result}</span>`;
      }
      let epl = "—", eplCls = "mut";
      if (p.expected_pl != null) {
        const s = p.expected_pl >= 0 ? "+" : "";
        epl = `${s}$${p.expected_pl.toFixed(2)}`;
        eplCls = p.expected_pl > 0 ? "ok" : p.expected_pl < 0 ? "bad" : "mut";
      }
      const tr = document.createElement("tr");
      tr.innerHTML = `<td class="mut">${t}</td><td>${p.asset}</td>
        <td class="${tgtCls}">${tgtArrow} ${p.target}</td>
        <td>${p.ask.toFixed(2)}</td>
        <td>${p.fair_p.toFixed(2)}</td>
        <td>${(p.edge*100).toFixed(1)}%</td>
        <td>$${p.size_usdc.toFixed(0)}</td>
        <td>${fmtPx(p.opening, p.asset)}</td>
        <td>${fmtPx(p.live, p.asset)}</td>
        <td>${expCell}</td>
        <td class="${eplCls}">${epl}</td>
        <td class="mut">${p.slug}</td>`;
      pendingBody.appendChild(tr);
    }

    for (const p of resolvedPicks) {
      const t = new Date(p.ts*1000).toLocaleTimeString();
      const plS = (p.pl>=0?"+":"") + "$" + p.pl.toFixed(2);
      const plCls = p.pl>0?"ok":p.pl<0?"bad":"mut";
      const tgtCls = p.target==="UP" ? "ok" : "bad";
      const tgtArrow = p.target==="UP" ? "↑" : "↓";
      const tr = document.createElement("tr");
      tr.innerHTML = `<td class="mut">${t}</td><td>${p.asset}</td>
        <td class="${tgtCls}">${tgtArrow} ${p.target}</td>
        <td>${p.ask.toFixed(2)}</td>
        <td>${p.fair_p.toFixed(2)}</td>
        <td>${(p.edge*100).toFixed(1)}%</td>
        <td>$${p.size_usdc.toFixed(0)}</td>
        <td>${fmtPx(p.opening, p.asset)}</td>
        <td><span class="pill ${p.result}">${p.result}</span></td>
        <td class="${plCls}">${plS}</td><td class="mut">${p.slug}</td>`;
      resBody.appendChild(tr);
    }

    const up = d.dashboard_uptime_sec;
    document.getElementById("uptime").textContent = dur(up);
    const botState = d.bot.sniper_service;
    const bs = document.getElementById("bot_status");
    bs.textContent = botState;
    bs.className = botState==="active" ? "ok" : "bad";
    const st = document.getElementById("status");
    st.textContent = botState==="active" ? "LIVE" : "OFFLINE";
    st.className = "pill " + (botState==="active" ? "WIN" : "LOSS");
    document.getElementById("clock").textContent = new Date().toLocaleString();
  } catch(e) {
    document.getElementById("status").textContent = "ERR";
  }
}
refresh(); setInterval(refresh, 500);
</script>
</body></html>
"""


@app.get("/", response_class=HTMLResponse)
def index(_=Depends(_check_auth)) -> HTMLResponse:
    return HTMLResponse(INDEX_HTML)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080, log_level="info", access_log=False)
