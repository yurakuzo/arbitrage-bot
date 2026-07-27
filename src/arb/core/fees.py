"""Per-venue fee models.

Fees decide whether an apparent price gap is *real* edge. These are pure functions
so they can be unit-tested deterministically.

IMPORTANT: the exact fee numbers change and vary per series/category. The Kalshi
convex formula is verified; Polymarket's 2026 schedule is NOT yet verified against
an official page (see docs/PROJECT.md). Treat Polymarket defaults as conservative
placeholders and confirm before Phase 4.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass


class FeeModel(ABC):
    """Cost, in dollars, to trade `contracts` at execution price `price` ([0,1])."""

    @abstractmethod
    def trade_fee(self, contracts: float, price: float, *, maker: bool = False) -> float:
        ...


@dataclass(frozen=True, slots=True)
class KalshiFeeModel(FeeModel):
    """Kalshi convex fee: ceil(rate * C * P * (1-P)) rounded up to the next cent.

    Default taker rate 0.07 peaks at $1.75 per 100 contracts at P=0.50 — exactly
    where cross-venue arbitrage tends to cluster. Some series use different
    multipliers and a reduced maker rate; override per series via config.
    """

    taker_rate: float = 0.07
    maker_rate: float = 0.0175

    def trade_fee(self, contracts: float, price: float, *, maker: bool = False) -> float:
        rate = self.maker_rate if maker else self.taker_rate
        raw_cents = rate * contracts * price * (1.0 - price) * 100.0
        # Round away binary FP noise (e.g. 175.0000000003) before ceiling to the
        # next whole cent, so P=0.50 -> exactly $1.75, not $1.76.
        return math.ceil(round(raw_cents, 6)) / 100.0


@dataclass(frozen=True, slots=True)
class PolymarketFeeModel(FeeModel):
    """Polymarket fee as basis points of notional (contracts * price).

    Historically ~0%. A 2026 schedule reportedly introduced small, category-
    dependent taker fees with maker rebates. Numbers here are placeholders —
    VERIFY against the official fee page before relying on them. Gas is
    relayer-subsidized and modeled separately (see docs/PROJECT.md).
    """

    taker_bps: float = 0.0
    maker_bps: float = 0.0

    def trade_fee(self, contracts: float, price: float, *, maker: bool = False) -> float:
        bps = self.maker_bps if maker else self.taker_bps
        notional = contracts * price
        return notional * bps / 10_000.0
