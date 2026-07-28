"""The pluggable Provider interface.

This is the single abstraction both modes (analysis + live) depend on. Core logic
never imports a concrete venue; adding a new venue means implementing this ABC once.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

from arb.core.fees import FeeModel
from arb.providers.models import Market, MarketBook, MarketFilter, Order, OrderResult, Venue

if TYPE_CHECKING:
    from arb.live.gate import TradingGate


class Provider(ABC):
    """A trading-venue adapter."""

    venue: Venue

    # --- Discovery (analysis mode) -------------------------------------------
    @abstractmethod
    async def list_markets(self, flt: MarketFilter) -> list[Market]:
        """Browse/enumerate markets matching a filter."""

    async def discover(self, limit: int, discovery: dict | None = None) -> list[Market]:
        """Return a universe of candidate markets for analysis mode.

        Default: a single top listing (fine for venues whose listing is already
        popularity-sorted, e.g. Polymarket by volume). Venues whose listing is
        dominated by noise override this — Kalshi sweeps curated series instead.
        """
        return await self.list_markets(MarketFilter(status="open", limit=limit))

    # --- Market data ----------------------------------------------------------
    @abstractmethod
    async def get_order_book(self, market_id: str) -> MarketBook:
        """Fetch a REST order-book snapshot for one market."""

    def stream_order_books(self, market_ids: list[str]) -> AsyncIterator[MarketBook]:
        """Yield live order-book updates over WebSocket (live mode).

        Implementations own reconnect/resync so callers just consume books.
        Returns an async iterator (async generator). Default is a Phase-3 stub so
        analysis-mode providers can be instantiated without a live feed yet.
        """
        raise NotImplementedError("Live WebSocket streaming is implemented in Phase 3.")

    # --- Fees -----------------------------------------------------------------
    @abstractmethod
    def fee_model(self, series_key: str | None = None) -> FeeModel:
        """Return the fee model for a market/series."""

    # --- Execution (Phase 4 — gated) -----------------------------------------
    async def place_order(self, order: Order, gate: TradingGate) -> OrderResult:
        """Place a REAL order. Implementations MUST call `gate.check_order(...)`
        first — the gate is the only thing standing between this call and real
        money, and there is no code path to a live order that skips it.

        The default raises: a venue without a real implementation can never trade.
        """
        raise NotImplementedError(f"Live order placement not implemented for {self.venue}.")

    async def cancel_order(self, order_id: str, gate: TradingGate) -> bool:
        raise NotImplementedError(f"Order cancel not implemented for {self.venue}.")
