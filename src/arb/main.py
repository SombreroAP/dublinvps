"""Convergence-arb bot — independent strategy.

Every active 5m crypto market: fetch live YES and NO CLOB books, compute
effective ask prices for a target position size. If buying BOTH sides at
those asks gives a guaranteed positive return (one side ALWAYS pays $1),
emit a paper signal.

Profit per $1 of payoff = 1 - (P_Y + P_N) - f(P_Y) - f(P_N)
  where f(p) = Polymarket dynamic taker fee (fraction of notional).

Not shadowing anyone — we detect the opportunity ourselves from live books.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
import orjson

from src.config import settings
from src.logging_setup import configure, log
from src.polymarket.gamma import Market, fetch_active_markets, fetch_clob_fill_ask

PAPER_LOG = Path("paper_trades_arb.jsonl")

# Tunables
ARB_MIN_PROFIT = 0.010        # fire when guaranteed profit > 1%
POSITION_USDC = 10.0          # desired fill size per side
GAMMA_TTL = 3.0
POLL_SEC = 1.0


def estimate_net_profit(yes_ask: float, no_ask: float,
                        fee_rate: float, fee_exp: float) -> float:
    """Guaranteed profit per $1 of payoff, after symmetric taker fees.
    Makers pay 0 fee + rebate so this is the conservative (taker) case."""
    def fee(p: float) -> float:
        if fee_rate <= 0:
            return 0.0
        shape = max(0.0, 1.0 - 4.0 * (p - 0.5) ** 2)
        return fee_rate * (shape ** fee_exp)
    cost = yes_ask * (1 + fee(yes_ask)) + no_ask * (1 + fee(no_ask))
    return 1.0 - cost


@dataclass(frozen=True)
class ArbSignal:
    slug: str
    asset: str
    yes_ask: float
    no_ask: float
    arb_spread: float     # yes_ask + no_ask
    gross_profit: float   # 1 - arb_spread
    net_profit: float     # after fees
    fillable_yes: float   # USDC absorbable on YES side at computed ask
    fillable_no: float    # USDC absorbable on NO side at computed ask
    sec_left: float


async def evaluate_market(
    client: httpx.AsyncClient, m: Market, size: float
) -> ArbSignal | None:
    """Fetch live fill asks for our target size on both sides."""
    if m.seconds_remaining < 10:
        return None  # too close to settlement, can't fill reliably
    (yes_fill, yes_best, yes_fillable), (no_fill, no_best, no_fillable) = await asyncio.gather(
        fetch_clob_fill_ask(client, m.yes_token_id, size),
        fetch_clob_fill_ask(client, m.no_token_id, size),
    )
    if yes_fill is None or no_fill is None:
        return None
    # Need depth on BOTH sides for a real arb
    if yes_fillable < size * 0.5 or no_fillable < size * 0.5:
        return None
    spread = yes_fill + no_fill
    gross = 1.0 - spread
    net = estimate_net_profit(yes_fill, no_fill, m.fee_rate, m.fee_exponent)
    if net < ARB_MIN_PROFIT:
        return None
    return ArbSignal(
        slug=m.slug, asset=m.asset,
        yes_ask=yes_fill, no_ask=no_fill,
        arb_spread=spread, gross_profit=gross, net_profit=net,
        fillable_yes=yes_fillable, fillable_no=no_fillable,
        sec_left=m.seconds_remaining,
    )


def signal_to_dict(sig: ArbSignal, size: float) -> dict:
    return {
        "ts": time.time(),
        "slug": sig.slug, "asset": sig.asset,
        "yes_ask": sig.yes_ask, "no_ask": sig.no_ask,
        "arb_spread": sig.arb_spread,
        "gross_profit_per_usd": sig.gross_profit,
        "net_profit_per_usd": sig.net_profit,
        "fillable_yes_usdc": sig.fillable_yes,
        "fillable_no_usdc": sig.fillable_no,
        "sec_left": sig.sec_left,
        "position_usdc_each_side": size,
        # Expected $ profit at position size (minimum of two fillable sides caps it)
        "expected_profit_usd": sig.net_profit * min(size, sig.fillable_yes, sig.fillable_no),
        "strategy": "arb_v1",
    }


async def main() -> None:
    configure()
    log.info("arb.startup", min_profit=ARB_MIN_PROFIT, size=POSITION_USDC)

    cache: dict = {"markets": [], "fetched_at": 0.0}
    # Dedup: one signal per (slug) per price-change (avoid duplicate ticks)
    last_sig: dict[str, tuple[float, float]] = {}
    last_status = 0.0
    fired_count = 0

    async with httpx.AsyncClient() as http:
        async def refresh_markets() -> list[Market]:
            now = asyncio.get_event_loop().time()
            if now - cache["fetched_at"] > GAMMA_TTL:
                try:
                    cache["markets"] = await fetch_active_markets(http, horizon_sec=600)
                    cache["fetched_at"] = now
                except Exception as e:
                    log.error("arb.gamma.error", error=str(e))
            return cache["markets"]

        while True:
            try:
                markets = await refresh_markets()
                tasks = [evaluate_market(http, m, POSITION_USDC) for m in markets]
                signals = await asyncio.gather(*tasks, return_exceptions=True)
                for m, sig in zip(markets, signals):
                    if isinstance(sig, Exception) or sig is None:
                        continue
                    fp = (round(sig.yes_ask, 4), round(sig.no_ask, 4))
                    if last_sig.get(sig.slug) == fp:
                        continue
                    last_sig[sig.slug] = fp
                    d = signal_to_dict(sig, POSITION_USDC)
                    PAPER_LOG.open("ab").write(orjson.dumps(d) + b"\n")
                    log.info("arb.signal", **{k: d[k] for k in (
                        "slug", "asset", "yes_ask", "no_ask", "arb_spread",
                        "net_profit_per_usd", "expected_profit_usd", "sec_left")})
                    fired_count += 1
                # Prune last_sig for finished markets
                live = {m.slug for m in markets}
                for k in list(last_sig):
                    if k not in live:
                        last_sig.pop(k, None)
                now = time.time()
                if now - last_status > 30:
                    log.info("arb.heartbeat", markets=len(markets),
                             tracked=len(last_sig), fired_total=fired_count)
                    last_status = now
            except Exception as e:
                log.error("arb.loop.error", error=str(e))
            await asyncio.sleep(POLL_SEC)


if __name__ == "__main__":
    asyncio.run(main())
