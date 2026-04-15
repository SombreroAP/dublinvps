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


def _fetch_resolution(slug: str) -> str:
    """Cached Gamma resolution lookup. ttl=60s."""
    now = time.time()
    hit = _resolution_cache.get(slug)
    if hit and now - hit[0] < 60:
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
            if not m.get("closed"):
                result = "open"
            else:
                op = json.loads(m.get("outcomePrices") or "[]")
                if op == ["1", "0"]:
                    result = "UP"
                elif op == ["0", "1"]:
                    result = "DOWN"
                else:
                    result = "unknown"
    except Exception as e:
        result = f"err"
    _resolution_cache[slug] = (now, result)
    return result


def _compute_backtest() -> dict:
    global _backtest_cache
    if _backtest_cache and time.time() - _backtest_cache[0] < 30:
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
        live = _chainlink.last_price.get(r["asset"])
        open_px = r.get("opening")
        delta = (live - open_px) if (live is not None and open_px is not None) else None
        # Is live trending toward winning? (only meaningful while open)
        if delta is None:
            trending = None
        else:
            trending = (target == "UP" and delta > 0) or (target == "DOWN" and delta < 0)
        picks.append({
            "ts": r["ts"], "slug": slug, "asset": r["asset"], "side": side,
            "target": target,
            "ask": r["ask"], "fair_p": r["fair_p"], "edge": r["edge"],
            "fee": r["fee"], "size_usdc": r["size_usdc"],
            "opening": open_px, "live": live, "delta": delta,
            "trending": trending,
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


def _bot_status() -> dict:
    try:
        out = subprocess.check_output(
            ["systemctl", "is-active", "sniper.service"], timeout=3, text=True
        ).strip()
    except Exception:
        out = "unknown"
    return {"sniper_service": out}


@app.get("/api/summary")
def api_summary(_=Depends(_check_auth)) -> JSONResponse:
    bt = _compute_backtest()
    return JSONResponse({
        "now": int(time.time()),
        "dashboard_uptime_sec": int(time.time() - _started),
        "bot": _bot_status(),
        "chainlink": {
            "connected": bool(_chainlink.last_price),
            "prices": _chainlink.last_price,
            "last_update_ms": _chainlink.last_ts_ms,
        },
        "backtest": bt,
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

<div class="card" style="margin:0 16px 16px">
  <h2>Recent picks (best per market, most recent first)</h2>
  <table id="picks_tbl"><thead>
    <tr><th>Time</th><th>Asset</th><th>Target</th><th>Ask</th><th>Fair p</th><th>Edge</th><th>Size</th><th>Open</th><th>Live</th><th>Δ</th><th>Result</th><th>P&amp;L</th><th>Slug</th></tr>
  </thead><tbody></tbody></table>
</div>
</div>

<div class="foot">
  <div>auto-refresh every 10s · dashboard uptime <span id="uptime">—</span></div>
  <div>bot: <span id="bot_status" class="mut">—</span></div>
</div>

<script>
function fmt(n, d=2) { return n==null ? "—" : n.toFixed(d); }
function dur(s) {
  const h = Math.floor(s/3600), m = Math.floor((s%3600)/60);
  return h>0 ? `${h}h${m}m` : `${m}m`;
}
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

    const tbody = document.querySelector("#picks_tbl tbody");
    tbody.innerHTML = "";
    const fmtPx = (v, a) => v==null ? "—" : (a==="SOL" ? v.toFixed(3) : v.toFixed(2));
    for (const p of bt.picks) {
      const tr = document.createElement("tr");
      const t = new Date(p.ts*1000).toLocaleTimeString();
      const plS = (p.pl>=0?"+":"") + "$" + p.pl.toFixed(2);
      const plCls = p.pl>0?"ok":p.pl<0?"bad":"mut";
      const tgtCls = p.target==="UP" ? "ok" : "bad";
      const tgtArrow = p.target==="UP" ? "↑" : "↓";
      let deltaCell = "—", deltaCls = "mut";
      // Only show Δ for OPEN positions — for resolved, post-round drift is misleading.
      if (p.delta != null && p.result === "open") {
        const sign = p.delta>=0 ? "+" : "";
        const deltaFmt = p.asset==="SOL" ? p.delta.toFixed(3) : p.delta.toFixed(2);
        deltaCell = `${sign}${deltaFmt}`;
        deltaCls = p.trending ? "ok" : "bad";
      }
      tr.innerHTML = `<td class="mut">${t}</td><td>${p.asset}</td>
        <td class="${tgtCls}">${tgtArrow} ${p.target}</td>
        <td>${p.ask.toFixed(2)}</td><td>${p.fair_p.toFixed(2)}</td>
        <td>${(p.edge*100).toFixed(1)}%</td>
        <td>$${p.size_usdc.toFixed(0)}</td>
        <td>${fmtPx(p.opening, p.asset)}</td>
        <td>${fmtPx(p.live, p.asset)}</td>
        <td class="${deltaCls}">${deltaCell}</td>
        <td><span class="pill ${p.result}">${p.result}</span></td>
        <td class="${plCls}">${plS}</td><td class="mut">${p.slug}</td>`;
      tbody.appendChild(tr);
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
refresh(); setInterval(refresh, 10000);
</script>
</body></html>
"""


@app.get("/", response_class=HTMLResponse)
def index(_=Depends(_check_auth)) -> HTMLResponse:
    return HTMLResponse(INDEX_HTML)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080, log_level="info", access_log=False)
