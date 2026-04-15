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
    max_position_usdc: float = 25.0
    entry_window_start_sec: int = 45
    entry_window_end_sec: int = 5

    # Per-second log-price volatility in bps. Used in Brownian fair_p model:
    #   σ_remaining_bps = σ_per_sqrt_sec × √seconds_left
    # Defaults are rough starting points from typical 5m realized vol.
    # Tune after collecting real data.
    sigma_bps_btc: float = 1.0
    sigma_bps_eth: float = 1.2
    sigma_bps_sol: float = 1.5

    # Sanity filter: if our model disagrees with the market by more than this
    # (in probability points), SKIP the signal. Large disagreements usually
    # mean the market knows something (e.g. Chainlink/Binance divergence,
    # news), not that we found an impossibly-good edge.
    max_disagreement: float = 0.30

    # Mode
    mode: str = Field(default="paper", pattern="^(paper|live)$")

    log_level: str = "INFO"


settings = Settings()
