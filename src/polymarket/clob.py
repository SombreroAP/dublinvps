"""Polymarket CLOB read-only wrapper. Live-trading methods stubbed for Phase 2."""
from __future__ import annotations

from dataclasses import dataclass

from py_clob_client.client import ClobClient

from src.config import settings


@dataclass(frozen=True)
class TopOfBook:
    bid: float | None
    ask: float | None

    @property
    def mid(self) -> float | None:
        if self.bid is None or self.ask is None:
            return None
        return (self.bid + self.ask) / 2


class PolyCLOB:
    """Thin wrapper over py-clob-client. Read-only for paper mode."""

    def __init__(self) -> None:
        self._client = ClobClient(
            host=settings.poly_clob_host,
            chain_id=settings.poly_chain_id,
            key=settings.poly_private_key or None,
            signature_type=0,  # pure EOA — see docs/polymarket-api-research.md
        )
        if settings.mode == "live" and settings.poly_private_key:
            self._client.set_api_creds(self._client.create_or_derive_api_creds())

    def top_of_book(self, token_id: str) -> TopOfBook:
        ob = self._client.get_order_book(token_id)
        bids = getattr(ob, "bids", []) or []
        asks = getattr(ob, "asks", []) or []
        # Order book convention: bids descending, asks ascending
        best_bid = float(bids[0].price) if bids else None
        best_ask = float(asks[0].price) if asks else None
        return TopOfBook(bid=best_bid, ask=best_ask)
