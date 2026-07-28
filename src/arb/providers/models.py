"""Normalized, venue-agnostic domain models.

Every provider translates its raw payloads into these types so that core logic
(pricing, sizing, matching) never depends on a specific venue's wire format.

Price convention: all prices are floats in dollars in [0.0, 1.0] where 1.0 == a
contract that settles for $1. Kalshi's cents (1..99) and Polymarket's USDC prices
are both normalized into this range at the provider boundary. `size` is the number
of contracts available at that price level.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class Venue(str, Enum):
    KALSHI = "kalshi"
    POLYMARKET = "polymarket"


class Outcome(str, Enum):
    """Binary outcome side. Non-binary markets are modeled as multiple binaries."""

    YES = "yes"
    NO = "no"


@dataclass(frozen=True, slots=True)
class PriceLevel:
    """One rung of an order book ladder."""

    price: float  # dollars, [0, 1]
    size: float  # contracts available at this price


@dataclass(slots=True)
class OutcomeBook:
    """Book for a single outcome token.

    `asks` = the ladder you consume to BUY this outcome (best/cheapest first).
    `bids` = the ladder you consume to SELL this outcome (best/highest first).

    Note on Kalshi: buying YES can be executed against resting NO bids
    (buy 1 YES == 100c - best NO bid). Providers are responsible for translating
    venue book semantics into this uniform "asks = cost to buy" convention.
    """

    asks: list[PriceLevel] = field(default_factory=list)
    bids: list[PriceLevel] = field(default_factory=list)

    @property
    def best_ask(self) -> float | None:
        return self.asks[0].price if self.asks else None

    @property
    def best_bid(self) -> float | None:
        return self.bids[0].price if self.bids else None


@dataclass(slots=True)
class MarketBook:
    """A snapshot of both outcome books for one market at one venue."""

    venue: Venue
    market_id: str  # venue-native id (Kalshi ticker / Polymarket token pair key)
    ts: datetime  # when the snapshot was observed (UTC)
    yes: OutcomeBook = field(default_factory=OutcomeBook)
    no: OutcomeBook = field(default_factory=OutcomeBook)

    def book_for(self, outcome: Outcome) -> OutcomeBook:
        return self.yes if outcome is Outcome.YES else self.no


@dataclass(slots=True)
class Market:
    """Venue-native market metadata (discovery result)."""

    venue: Venue
    market_id: str
    title: str
    close_time: datetime | None = None
    # Grouping keys for recurring/repeatable markets (e.g. Kalshi series ticker).
    series_key: str | None = None
    # Free-form venue extras kept for debugging / later normalization.
    raw: dict = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class MarketFilter:
    """Filter passed to Provider.list_markets."""

    query: str | None = None
    series_key: str | None = None
    status: str | None = "open"
    limit: int | None = None


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


class TimeInForce(str, Enum):
    GTC = "gtc"  # good-till-cancel
    IOC = "ioc"  # immediate-or-cancel / fill-and-kill
    FOK = "fok"  # fill-or-kill


@dataclass(frozen=True, slots=True)
class Order:
    """A venue-agnostic order request (Phase 4).

    `limit_price` is in dollars [0,1]. For arbitrage we place limit orders at (or
    just through) the observed ask so fills are near the priced level; a marketable
    limit with IOC/FOK avoids resting exposure on one leg.
    """

    venue: Venue
    market_id: str
    outcome: Outcome
    side: Side
    contracts: float
    limit_price: float
    tif: TimeInForce = TimeInForce.FOK
    client_order_id: str | None = None


@dataclass(frozen=True, slots=True)
class OrderResult:
    """Outcome of a place_order call."""

    ok: bool
    order_id: str | None = None
    filled_contracts: float = 0.0
    avg_price: float | None = None
    status: str = ""
    raw: dict = field(default_factory=dict)
