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

    # Mode
    mode: str = Field(default="paper", pattern="^(paper|live)$")

    log_level: str = "INFO"


settings = Settings()
