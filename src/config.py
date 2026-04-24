"""Centralized config loaded from env. Import `settings` anywhere."""
from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Polymarket
    poly_private_key: str = ""
    poly_funder_address: str = ""
    poly_signature_type: int = 1  # 0=EOA, 1=Magic proxy, 2=Safe proxy
    poly_chain_id: int = 137
    poly_clob_host: str = "https://clob.polymarket.com"
    poly_gamma_host: str = "https://gamma-api.polymarket.com"

    # Strategy
    edge_threshold: float = 0.025
    max_position_usdc: float = 25.0   # hard per-trade ceiling
    bankroll_usdc: float = 100.0      # total allocated capital (Kelly base)
    # Fractional Kelly multiplier. 0.5 = half-Kelly (industry standard:
    # ~99% of geometric growth at ~half the variance of full-Kelly).
    # Lower = safer / less aggressive sizing.
    kelly_fraction: float = 0.5
    entry_window_start_sec: int = 45
    entry_window_end_sec: int = 5

    # Per-second log-price volatility in bps. Used in Brownian fair_p model:
    #   σ_remaining_bps = σ_per_sqrt_sec × √seconds_left
    # Defaults are rough starting points from typical 5m realized vol.
    # Tune after collecting real data.
    sigma_bps_btc: float = 1.0
    sigma_bps_eth: float = 1.2
    sigma_bps_sol: float = 1.5

    # Safety multiplier applied to σ in fair_p. Accounts for crypto fat tails
    # and mean reversion that Brownian model misses. 1.5 = 50% wider
    # confidence intervals than pure-Gaussian realized vol implies.
    sigma_safety_mult: float = 1.5

    # Hard cap on fair_p (and symmetric floor 1 - cap for NO side). Prevents
    # the model from claiming absurd confidence (0.99+) that tail events
    # regularly violate. 0.95 ≈ our empirical win rate.
    fair_p_cap: float = 0.95

    # Sanity filter: if our model disagrees with the market by more than this
    # (in probability points), SKIP the signal. Large disagreements usually
    # mean the market knows something (e.g. Chainlink/Binance divergence,
    # news), not that we found an impossibly-good edge.
    max_disagreement: float = 0.30

    # Minimum fair_p to fire a signal. Data: fair_p<0.85 wins ~25%, fair_p>=0.9
    # wins ~87%. Below this threshold our Brownian model is too noisy to trust.
    min_fair_p: float = 0.85

    # Require the Chainlink move to be at least N standard deviations from 0
    # relative to remaining volatility. |z| = move_bps / (sigma * sqrt(sec_left)).
    # z >= 2.0 means "move is too big to plausibly reverse from noise alone."
    # Data showed 4/4 historical losses had |z|<1.5 (fragile tiny moves).
    min_z_score: float = 2.0

    # Sides enabled. Data as of first 34 realistic picks: YES won 75% / +$90,
    # NO won 57% / -$95. Turning NO off removed the losing leg entirely.
    # Change to "YES,NO" once/if NO performance improves.
    enabled_sides: str = "YES"

    # Cross-feed sanity check. If our Chainlink "current" price diverges from
    # Binance mid by more than this many bps, SKIP the signal — Chainlink is
    # likely lagging during a fast move (we lost a SOL pick to exactly this:
    # Binance was crashing at -4bps while Chainlink still showed +9bps).
    # Set to 0 to disable.
    max_feed_divergence_bps: float = 5.0

    # Trajectory / momentum filter.
    # Lookback window (seconds) for computing recent price velocity.
    trajectory_lookback_sec: float = 5.0
    # If we want to bet UP but price is falling faster than this (bps/sec),
    # skip — momentum is against us, move is likely reversing. Symmetric for
    # DOWN bets. Set to 0 to disable filter (but still log velocity).
    max_counter_trajectory_bps_per_sec: float = 0.5

    # Correlation cap. BTC/ETH/SOL on 5m windows are ~95% correlated; when
    # one's wrong they all tend to be wrong. A 3-asset correlated loss wipes
    # ~2-3 weeks of tiny wins given our 14:1 loss/win ratio. Cap = max picks
    # per round across ALL assets. 1 = diversified in time, not in assets.
    # 0 disables (revert to per-asset behavior).
    max_picks_per_round: int = 1

    # === ACTIVE EXIT MANAGEMENT ===
    # Take-profit: exit when CLOB bid rises to entry_ask * (1 + TP_PCT).
    # E.g. 0.10 = exit when bid = entry × 1.10.
    take_profit_pct: float = 0.10
    # Stop-loss: exit when CLOB bid falls to entry_ask * (1 + SL_PCT).
    # E.g. -0.05 = cut losses when bid drops 5% from entry.
    stop_loss_pct: float = -0.05
    # How often (seconds) to poll the CLOB book for held positions.
    exit_poll_interval_sec: float = 1.0

    # Mode
    mode: str = Field(default="paper", pattern="^(paper|live)$")

    log_level: str = "INFO"


settings = Settings()
