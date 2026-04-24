"""Edge calculation + paper-trade signal logger.

ACTIVE EXIT MANAGEMENT strategy:
- Entry: same as before (z-score, edge, velocity, etc. — all filter gates).
- Held as a Position until:
    * CLOB bid reaches entry_ask × (1 + TAKE_PROFIT_PCT) → sell, book profit
    * CLOB bid falls to entry_ask × (1 + STOP_LOSS_PCT)  → sell, cut loss
    * Round ends without either → fall back to binary resolution (win $1 or $0)

Paper log is append-only JSONL with two event types sharing `position_id`:
    entry:        when we open (single line per opened position)
    exit_<kind>:  when we close (tp / sl / resolve / expired)
Backtest pairs them by position_id to compute full round-trip P&L.
"""
from __future__ import annotations

import asyncio
import subprocess
import time
from dataclasses import dataclass, field
from math import erf, sqrt
from pathlib import Path
from typing import Literal

import httpx
import orjson

from src.config import settings
from src.logging_setup import log
from src.polymarket.gamma import Market, fetch_clob_book, sweep_fill_ask

PAPER_LOG = Path("paper_trades.jsonl")


ExitKind = Literal["tp", "sl", "resolve", "expired"]


@dataclass
class Position:
    """An open paper position we're actively managing for TP/SL/resolution."""
    position_id: str           # slug + side + entry_ts_ms
    slug: str
    asset: str
    side: str                  # YES or NO
    token_id: str              # CLOB token id for the bought side
    entry_ts: float
    round_start: int
    round_end: int
    entry_ask: float           # VWAP we "paid" (includes fee already in cost)
    entry_fee: float           # fraction of notional
    size_usdc: float           # notional staked
    shares: float              # size_usdc / entry_ask
    tp_bid: float              # bid threshold to sell at take profit
    sl_bid: float              # bid threshold to sell at stop loss

    @property
    def entry_cost_usdc(self) -> float:
        """What leaving entry cost us: stake + taker fee."""
        return self.size_usdc + (self.size_usdc * self.entry_fee)


def _sigma_bps(asset: str) -> float:
    return {
        "BTC": settings.sigma_bps_btc,
        "ETH": settings.sigma_bps_eth,
        "SOL": settings.sigma_bps_sol,
    }.get(asset, 1.2)


def _phi(z: float) -> float:
    """Standard normal CDF."""
    return 0.5 * (1.0 + erf(z / sqrt(2)))


def compute_z(asset: str, current: float, opening: float,
              seconds_left: float) -> tuple[float, float, float]:
    """Return (z_score, move_bps, sd_remaining_bps). z = move / (σ·mult·√(T-s)).
    σ is inflated by SIGMA_SAFETY_MULT — this widens the noise budget so
    marginal moves correctly look fragile instead of decisive.
    """
    if opening <= 0 or seconds_left <= 0:
        return 0.0, 0.0, 0.0
    move_bps = (current - opening) / opening * 10_000
    sigma = _sigma_bps(asset) * settings.sigma_safety_mult
    sd_remaining_bps = sigma * sqrt(seconds_left)
    if sd_remaining_bps < 1e-9:
        return (float("inf") if move_bps > 0 else float("-inf") if move_bps < 0 else 0.0,
                move_bps, sd_remaining_bps)
    return move_bps / sd_remaining_bps, move_bps, sd_remaining_bps


def fair_yes_probability(asset: str, current: float, opening: float,
                         seconds_left: float) -> float:
    """P(close ≥ open | current move so far, time remaining) under a zero-drift
    Brownian model, with two crypto-specific adjustments:

    1. σ inflated by SIGMA_SAFETY_MULT (handled in compute_z) — accounts for
       fat tails and mean reversion the pure model misses.
    2. Output clamped to [1-FAIR_P_CAP, FAIR_P_CAP] — the model never claims
       confidence above our empirical win rate, preventing overconfident
       entries on near-certain-looking trades that turn out to be fragile.
    """
    if seconds_left <= 0:
        # At expiry, outcome is known; still clamp so evaluate() uses
        # consistent threshold logic for the "edge" calculation.
        p = 1.0 if current >= opening else 0.0
    else:
        z, _, sd = compute_z(asset, current, opening, seconds_left)
        if sd < 1e-9:
            p = 1.0 if current >= opening else 0.0
        else:
            p = _phi(z)
    cap = settings.fair_p_cap
    return max(1.0 - cap, min(cap, p))


async def evaluate_and_log(
    market: Market,
    current_price: float,
    opening_price: float,
    http: httpx.AsyncClient,
    last_sig: dict,
    round_fired: set,
    open_positions: dict,
    binance_price: float | None = None,
    velocity_bps_per_sec: float | None = None,
) -> None:
    """Evaluate a single market. If it passes the edge gate, re-fetch a LIVE
    CLOB book for the relevant side, then log with fresh prices."""
    sec_left = market.seconds_remaining
    if not (settings.entry_window_end_sec < sec_left <= settings.entry_window_start_sec):
        return

    # z-score gate: the move so far must be statistically significant vs the
    # expected remaining volatility. Small moves with lots of time left are
    # fragile — they reverse. Data showed 4/4 historical losses had |z|<1.5.
    z, move_bps, sd_rem = compute_z(market.asset, current_price,
                                     opening_price, sec_left)
    if abs(z) < settings.min_z_score:
        return

    p_yes = fair_yes_probability(market.asset, current_price, opening_price, sec_left)

    # Use the cached ask to decide if signal-worthy; refetch live for logging.
    allowed = {s.strip() for s in settings.enabled_sides.split(",")}
    side = None
    if p_yes > 0.5 and market.best_ask_yes is not None and "YES" in allowed:
        side = "YES"
        cached_ask = market.best_ask_yes
        target_p = p_yes
        token_id = market.yes_token_id
    elif p_yes < 0.5 and market.best_ask_no is not None and "NO" in allowed:
        side = "NO"
        cached_ask = market.best_ask_no
        target_p = 1 - p_yes
        token_id = market.no_token_id
    if side is None:
        return

    # Require high model confidence. Data showed fair_p<0.85 wins ~25%.
    if target_p < settings.min_fair_p:
        return

    # Cross-feed sanity check. If our Chainlink "current" diverges from
    # Binance mid by too much, Chainlink is probably stale (this caused
    # our SOL loss — Chainlink lagged a real crash by ~15s). Skip.
    feed_div_bps = None
    if binance_price is not None and settings.max_feed_divergence_bps > 0:
        feed_div_bps = (current_price - binance_price) / binance_price * 10_000
        if abs(feed_div_bps) > settings.max_feed_divergence_bps:
            return

    # Trajectory filter: if momentum is strongly against our bet direction,
    # the move we're betting on is likely reversing. Hypothesis from loss
    # analysis: many losses had positive move_bps but price was ALREADY
    # turning at entry time. Skip these.
    v = velocity_bps_per_sec
    v_aligned: bool | None = None
    if v is not None:
        want_up = (side == "YES")
        v_aligned = (want_up and v >= 0) or (not want_up and v <= 0)
        threshold = settings.max_counter_trajectory_bps_per_sec
        if threshold > 0:
            if want_up and v < -threshold:
                return
            if (not want_up) and v > threshold:
                return

    # Cheap pre-filter: if cached ask is already way above fair, don't even
    # bother with the live refetch. Leave a small tolerance since fresh price
    # could be slightly cheaper.
    cached_edge = target_p - cached_ask - market.taker_fee_at(cached_ask)
    if cached_edge < settings.edge_threshold - 0.03:
        return

    # Live book refetch with DEPTH. Compute:
    #   fill_ask    — VWAP price we'd actually pay for our desired size
    #   market_mid  — (best_bid + best_ask)/2 of the side we're buying; this
    #                 is the market's own implied probability that our side wins.
    # If our fair_p disagrees with the market by too much, SKIP — the market
    # likely has information (e.g. Binance/Chainlink divergence) we don't.
    refetch_start = time.monotonic()
    desired_size = settings.max_position_usdc
    bids, asks = await fetch_clob_book(http, token_id)
    price_age_ms = int((time.monotonic() - refetch_start) * 1000)
    if not asks:
        return
    best_ask = asks[0][0]
    best_bid = bids[0][0] if bids else None
    fill_ask, fillable = sweep_fill_ask(asks, desired_size)
    if fill_ask is None or fillable < 5.0:  # dust book
        return
    # Reality filters — prevent logging signals that couldn't be filled:
    #   1. Ask below $0.05: typically a handful of dust shares; real queue
    #      behind them means we'd never hit this price at meaningful size.
    #   2. Claimed edge > 30%: if the market is ACTUALLY pricing 30pp below
    #      our fair_p, we are wrong, not the market. Likely stale data.
    #   3. Require at least 80% of desired size fillable.
    if fill_ask < 0.05:
        return
    if fillable < 0.8 * desired_size:
        return
    # Market's implied probability for OUR side. Use mid if we have both,
    # else fall back to best_ask.
    market_implied = ((best_bid + best_ask) / 2) if (best_bid is not None) else best_ask
    disagreement = abs(target_p - market_implied)
    if disagreement > settings.max_disagreement:
        return

    fee = market.taker_fee_at(fill_ask)
    edge = target_p - fill_ask - fee
    if edge <= settings.edge_threshold:
        return
    # Hard cap: edge > 30% almost certainly means stale data or a model bug,
    # not real alpha. Markets don't leave 30pp on the table for seconds.
    if edge > 0.30:
        return

    # Correlation cap: BTC/ETH/SOL move together on 5m windows. If we've
    # already fired for this round on any asset, skip — avoids 3-asset
    # correlated losses (biggest historical risk, 14:1 loss/win asymmetry).
    # Allow repeat fires for the SAME (slug, side) so dedup fingerprint can
    # still update an existing pick with better fill_ask.
    round_start = market.end_ts - 300  # 5m markets
    key = (market.slug, side)
    already_seen_same = key in last_sig
    if (settings.max_picks_per_round > 0
            and round_start in round_fired
            and not already_seen_same):
        return

    # Tighter dedup: fingerprint on fill_ask rounded to 2dp + fair_p to 2dp.
    # Small orderbook wobbles no longer trigger new log lines.
    fingerprint = (round(fill_ask, 2), round(target_p, 2))
    if last_sig.get(key) == fingerprint:
        return
    last_sig[key] = fingerprint
    round_fired.add(round_start)

    # Half-Kelly sizing.
    # For binary contract bought at price p with fair win-prob q (and fee
    # already subtracted from edge), the Kelly-optimal fraction of bankroll
    # is f* = edge / (1 - p). Half-Kelly = 0.5 * f* gives ~99% of geometric
    # growth at half the variance.
    kelly_full = edge / (1 - fill_ask) if fill_ask < 1 else 0.0
    kelly_size = settings.bankroll_usdc * kelly_full * settings.kelly_fraction
    size_usdc = max(market.min_size,
                    min(fillable, settings.max_position_usdc, kelly_size))

    # Don't open a new position if we already hold one for this (slug, side).
    position_key = f"{market.slug}:{side}"
    if position_key in open_positions:
        return

    # Compute TP/SL bid thresholds. Buying at fill_ask → we'll sell at some bid.
    # TP: entry_ask × (1 + tp_pct). E.g. 0.80 → 0.88 (10% gain).
    # SL: entry_ask × (1 + sl_pct). E.g. 0.80 → 0.76 (5% loss cap, assumes sl_pct < 0).
    # Bids are naturally ≤ ask so TP requires the market to LIFT past our entry.
    tp_bid = min(0.99, fill_ask * (1.0 + settings.take_profit_pct))
    sl_bid = max(0.01, fill_ask * (1.0 + settings.stop_loss_pct))
    shares = size_usdc / fill_ask if fill_ask > 0 else 0.0

    now_ts = time.time()
    position_id = f"{market.slug}:{side}:{int(now_ts * 1000)}"
    position = Position(
        position_id=position_id,
        slug=market.slug,
        asset=market.asset,
        side=side,
        token_id=token_id,
        entry_ts=now_ts,
        round_start=round_start,
        round_end=market.end_ts,
        entry_ask=fill_ask,
        entry_fee=fee,
        size_usdc=size_usdc,
        shares=shares,
        tp_bid=tp_bid,
        sl_bid=sl_bid,
    )
    open_positions[position_key] = position

    entry = {
        "event": "entry",
        "position_id": position_id,
        "ts": now_ts, "slug": market.slug, "asset": market.asset,
        "side": side,
        "ask": fill_ask,
        "best_ask": best_ask, "best_bid": best_bid,
        "market_implied_p": market_implied,
        "disagreement": disagreement,
        "cached_ask_at_discovery": cached_ask,
        "fillable_usdc": fillable,
        "price_age_ms": price_age_ms,
        "fair_p": target_p, "edge": edge, "fee": fee,
        "z_score": z, "move_bps": move_bps, "sd_remaining_bps": sd_rem,
        "current": current_price, "opening": opening_price, "sec_left": sec_left,
        "binance_price": binance_price,
        "feed_divergence_bps": feed_div_bps,
        "velocity_bps_per_sec": v,
        "velocity_aligned": v_aligned,
        "kelly_full_frac": kelly_full,
        "kelly_size_uncapped": kelly_size,
        "size_usdc": size_usdc,
        "shares": shares,
        "tp_bid": tp_bid,
        "sl_bid": sl_bid,
        "round_start": round_start,
        "round_end": market.end_ts,
    }
    PAPER_LOG.open("ab").write(orjson.dumps(entry) + b"\n")
    log.info("paper.entry", **entry)


def _fetch_round_outcome_sync(slug: str) -> str | None:
    """Return 'UP' / 'DOWN' / None (not yet resolved / error).
    Uses curl because httpx in-process can hit Cloudflare TLS fingerprint issues
    during heavy concurrent polling of CLOB."""
    try:
        raw = subprocess.check_output([
            "curl", "-s", "-H", "User-Agent: Mozilla/5.0",
            f"https://gamma-api.polymarket.com/events?slug={slug}",
        ], timeout=4)
        d = orjson.loads(raw)
        if not d:
            return None
        m = (d[0].get("markets") or [{}])[0]
        if not m.get("closed"):
            return None
        op = orjson.loads(m.get("outcomePrices") or "[]")
        if op == ["1", "0"]:
            return "UP"
        if op == ["0", "1"]:
            return "DOWN"
    except Exception:
        return None
    return None


def _log_exit(position: "Position", kind: ExitKind, exit_bid: float | None,
              exit_proceeds: float, exit_fee: float, net_pl: float,
              outcome: str | None = None) -> None:
    exit_event = {
        "event": f"exit_{kind}",
        "position_id": position.position_id,
        "ts": time.time(),
        "entry_ts": position.entry_ts,
        "slug": position.slug,
        "asset": position.asset,
        "side": position.side,
        "entry_ask": position.entry_ask,
        "size_usdc": position.size_usdc,
        "shares": position.shares,
        "exit_bid": exit_bid,
        "exit_proceeds_usdc": exit_proceeds,
        "exit_fee": exit_fee,
        "entry_cost_usdc": position.entry_cost_usdc,
        "net_pl_usdc": net_pl,
        "hold_sec": time.time() - position.entry_ts,
        "outcome": outcome,
    }
    PAPER_LOG.open("ab").write(orjson.dumps(exit_event) + b"\n")
    log.info(f"paper.exit.{kind}", **exit_event)


async def poll_exits(
    http: httpx.AsyncClient,
    open_positions: dict,
) -> None:
    """For each open position: fetch CLOB bid, check TP/SL/expiry conditions.
    Closes positions that hit any exit criteria and logs the exit."""
    if not open_positions:
        return

    async def check_one(key: str, pos: "Position") -> None:
        now = time.time()

        # Always fetch the CLOB book; we'll decide exit reason from it.
        bids, asks = await fetch_clob_book(http, pos.token_id)
        best_bid = bids[0][0] if bids else None

        # 1) FORCE EXIT window: we MUST close before round resolves. Even if
        # TP/SL hasn't triggered, exit now at whatever bid exists.
        force_exit_boundary = pos.round_end - settings.force_exit_sec_before_end
        if now >= force_exit_boundary:
            if best_bid is not None:
                fee_rate_at_bid = 0.072 * max(0.0, 1.0 - 4.0 * (best_bid - 0.5) ** 2)
                gross = pos.shares * best_bid
                exit_fee_usdc = gross * fee_rate_at_bid
                exit_proceeds = gross - exit_fee_usdc
                net_pl = exit_proceeds - pos.entry_cost_usdc
                _log_exit(pos, "expired", best_bid, exit_proceeds, fee_rate_at_bid, net_pl)
                open_positions.pop(key, None)
                return
            # Book has no bids AND round about to resolve — as absolute last
            # resort, use Chainlink-derived binary outcome directly (no Gamma
            # dependency). Compute from opening_at on our feed history.
            if now >= pos.round_end:
                # Won't happen unless the book went totally empty; fall back.
                outcome = _fetch_round_outcome_sync(pos.slug)
                if outcome is None:
                    return  # try next tick; we still haven't resolved
                won = (pos.side == "YES" and outcome == "UP") or \
                      (pos.side == "NO" and outcome == "DOWN")
                exit_proceeds = pos.shares if won else 0.0
                net_pl = exit_proceeds - pos.entry_cost_usdc
                _log_exit(pos, "resolve", None, exit_proceeds, 0.0, net_pl, outcome)
                open_positions.pop(key, None)
            return

        # 2) TP/SL checks (before force-exit window).
        if best_bid is None:
            return

        exit_kind: ExitKind | None = None
        if best_bid >= pos.tp_bid:
            exit_kind = "tp"
        elif best_bid <= pos.sl_bid:
            exit_kind = "sl"

        if exit_kind is None:
            return

        # Sell at best_bid. For paper we assume top-of-book fills cleanly for
        # our shares (usually true given our $25 size vs typical book depth).
        fee_rate_at_bid = 0.072 * max(0.0, 1.0 - 4.0 * (best_bid - 0.5) ** 2)
        gross = pos.shares * best_bid
        exit_fee_usdc = gross * fee_rate_at_bid
        exit_proceeds = gross - exit_fee_usdc
        net_pl = exit_proceeds - pos.entry_cost_usdc
        _log_exit(pos, exit_kind, best_bid, exit_proceeds, fee_rate_at_bid,
                  net_pl)
        open_positions.pop(key, None)

    await asyncio.gather(
        *(check_one(k, p) for k, p in list(open_positions.items())),
        return_exceptions=True,
    )


async def run_loop(feed, markets_provider, binance=None) -> None:
    last_status = 0.0
    last_sig: dict = {}
    round_fired: set[int] = set()
    # Active positions we're managing: key f"{slug}:{side}" → Position
    open_positions: dict[str, "Position"] = {}
    last_exit_poll = 0.0
    async with httpx.AsyncClient() as http:
        while True:
            try:
                markets, openings = await markets_provider()
                now = time.time()
                live_slugs = {m.slug for m in markets}
                live_rounds = {m.end_ts - 300 for m in markets}
                for k in list(last_sig):
                    if k[0] not in live_slugs:
                        last_sig.pop(k, None)
                # Drop expired rounds so the set doesn't grow forever.
                round_fired.intersection_update(live_rounds)

                # Exit management: poll open positions for TP/SL/resolve.
                # Runs at its own cadence (default 1s), not every tick.
                if now - last_exit_poll >= settings.exit_poll_interval_sec:
                    await poll_exits(http, open_positions)
                    last_exit_poll = now

                # Evaluate all markets concurrently — lets live refetches run
                # in parallel so we don't serialize 9+ HTTP calls.
                tasks = []
                for m in markets:
                    cur = feed.last_price.get(m.asset)
                    opn = openings.get(m.slug)
                    if cur is None or opn is None:
                        continue
                    bn = binance.last_price.get(m.asset) if binance is not None else None
                    # Trajectory: recent velocity of the Chainlink price feed.
                    v = None
                    if hasattr(feed, "velocity_bps_per_sec"):
                        v = feed.velocity_bps_per_sec(
                            m.asset, settings.trajectory_lookback_sec)
                    tasks.append(evaluate_and_log(
                        m, cur, opn, http, last_sig, round_fired,
                        open_positions, bn, v))
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)

                if now - last_status > 30:
                    in_window = sum(
                        1 for m in markets
                        if settings.entry_window_end_sec < m.seconds_remaining
                           <= settings.entry_window_start_sec
                    )
                    log.info("loop.heartbeat", markets=len(markets),
                             in_window=in_window,
                             open_positions=len(open_positions),
                             prices={a: feed.last_price.get(a)
                                     for a in ("BTC", "ETH", "SOL")})
                    last_status = now
            except Exception as e:
                log.error("loop.error", error=str(e))
            await asyncio.sleep(0.5)
