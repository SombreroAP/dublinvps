"""Ladder-fade strategy — inspired by wallet 0x7Da07B2a...

Hypothesis: when price has moved statistically far at T-90s to T-45s, the
market prices the favored side near 1.0. The OPPOSITE side trades at 0.01-
0.03. If the move partly reverts in the remaining time, those cheap shares
pay $1 each → ~50-100× returns on the cheap shares.

Signal: |z| > Z_THRESHOLD where z = move_bps / (σ·√sec_left).
Side:   ladder the OPPOSITE of current move direction.
Ladder: $0.03 → $0.02 → $0.01 with sizes in USDC (small — lottery tickets).

This file is purely signal logic. Fill simulation happens at resolution time.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from math import sqrt

from src.config import settings
from src.polymarket.gamma import Market


# Tunables. Start conservative; we'll calibrate from paper data.
Z_THRESHOLD = 2.0          # only ladder when move ≥ 2σ extreme
WINDOW_START_SEC = 90       # look at markets from T-90s
WINDOW_END_SEC = 45         # stop placing new ladders at T-45s (last-minute is expensive)
LADDER_LEVELS = [
    # (price, usdc_size)  — rough mirror of Dimpled-Dill's sizing
    (0.03, 1.00),
    (0.02, 2.00),
    (0.01, 5.00),
]


def _sigma_bps(asset: str) -> float:
    return {
        "BTC": settings.sigma_bps_btc,
        "ETH": settings.sigma_bps_eth,
        "SOL": settings.sigma_bps_sol,
    }.get(asset, 1.2)


@dataclass(frozen=True)
class LadderSignal:
    slug: str
    asset: str
    side: str                 # "YES" (Up) or "NO" (Down) — the side we'd BUY
    z_score: float
    move_bps: float
    sec_left: float
    opening: float
    current: float
    levels: list[tuple[float, float]]  # [(price, usdc), ...]

    @property
    def total_usdc(self) -> float:
        return sum(usdc for _, usdc in self.levels)


def evaluate(market: Market, current: float, opening: float) -> LadderSignal | None:
    """Return LadderSignal if conditions met, else None."""
    sec_left = market.seconds_remaining
    if not (WINDOW_END_SEC <= sec_left <= WINDOW_START_SEC):
        return None
    if opening is None or current is None or opening <= 0:
        return None
    move_bps = (current - opening) / opening * 10_000
    sigma = _sigma_bps(market.asset)
    sd = sigma * sqrt(max(sec_left, 1))
    if sd < 1e-9:
        return None
    z = move_bps / sd
    if abs(z) < Z_THRESHOLD:
        return None
    # Fade: bet the OPPOSITE of the move direction.
    # If move is positive (price up), fade = bet DOWN → buy NO
    # If move is negative (price down), fade = bet UP → buy YES
    side = "NO" if z > 0 else "YES"
    return LadderSignal(
        slug=market.slug, asset=market.asset, side=side,
        z_score=z, move_bps=move_bps, sec_left=sec_left,
        opening=opening, current=current,
        levels=list(LADDER_LEVELS),
    )


def signal_to_dict(sig: LadderSignal) -> dict:
    return {
        "ts": time.time(), "slug": sig.slug, "asset": sig.asset,
        "side": sig.side, "z_score": sig.z_score, "move_bps": sig.move_bps,
        "sec_left": sig.sec_left,
        "opening": sig.opening, "current": sig.current,
        "levels": sig.levels, "total_usdc": sig.total_usdc,
        "strategy": "ladder_fade_v1",
    }
