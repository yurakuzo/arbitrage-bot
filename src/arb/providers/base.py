"""The pluggable Provider interface.

This is the single abstraction both modes (analysis + live) depend on. Core logic
never imports a concrete venue; adding a new venue means implementing this ABC once.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

from arb.core.fees import FeeModel
from arb.providers.models import Market, MarketBook, MarketFilter, Venue


class Provider(ABC):
    """A trading-venue adapter."""

    venue: Venue

    # --- Discovery (analysis mode) -------------------------------------------
    @abstractmethod
    async def list_markets(self, flt: MarketFilter) -> list[Market]:
        """Browse/enumerate markets matching a filter."""

    # --- Market data ----------------------------------------------------------
    @abstractmethod
    async def get_order_book(self, market_id: str) -> MarketBook:
        """Fetch a REST order-book snapshot for one market."""

    @abstractmethod
    def stream_order_books(self, market_ids: list[str]) -> AsyncIterator[MarketBook]:
        """Yield live order-book updates over WebSocket (live mode).

        Implementations own reconnect/resync so callers just consume books.
        Returns an async iterator (async generator).
        """

    # --- Fees -----------------------------------------------------------------
    @abstractmethod
    def fee_model(self, series_key: str | None = None) -> FeeModel:
        """Return the fee model for a market/series."""

    # --- Execution (Phase 4 — intentionally unimplemented in v1) --------------
    async def place_order(self, *args, **kwargs):  # noqa: ANN002, ANN003
        raise NotImplementedError(
            "Live order placement is disabled in v1 (paper-trading only). "
            "It is a later, explicitly config-gated phase."
        )

    async def cancel_order(self, *args, **kwargs):  # noqa: ANN002, ANN003
        raise NotImplementedError("Live order placement is disabled in v1.")
