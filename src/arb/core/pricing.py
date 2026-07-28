"""Cross-venue arbitrage pricing.

For the SAME binary outcome on two venues, buying YES on one and NO on the other
locks in a payout of exactly $1 per matched pair regardless of resolution:

    profit_per_pair = 1 - (yes_ask_A + no_ask_B) - fees

An opportunity exists when the fee-net figure is positive. We evaluate both
directions (YES@A+NO@B and NO@A+YES@B) and return the better one.

Two entry points:
  - `top_of_book_edge`  : quick edge from best-ask numbers (used for ranking
                          historical snapshots, where we only stored top-of-book).
  - `find_arbitrage`    : walks both order books to find the executable size and
                          net profit (used live in Phase 3 with fresh books).

Fees decide reality: Kalshi's convex fee peaks near $0.50, so a 2¢ gross gap can
be entirely eaten. Always rank on the net figures here, never gross.
"""

from __future__ import annotations

from dataclasses import dataclass

from arb.core.fees import FeeModel
from arb.providers.models import MarketBook, Outcome, OutcomeBook, Venue


@dataclass(frozen=True, slots=True)
class ArbLeg:
    venue: Venue
    outcome: Outcome
    contracts: float
    avg_price: float
    cost: float  # contracts * avg_price
    fee: float


@dataclass(frozen=True, slots=True)
class ArbOpportunity:
    """Best executable arbitrage between two venues, net of fees."""

    contracts: float
    gross_profit: float  # contracts - total cost (before fees)
    net_profit: float  # after fees
    roi: float  # net_profit / total cost
    net_edge_per_contract: float  # net_profit / contracts
    legs: tuple[ArbLeg, ArbLeg]

    @property
    def is_profitable(self) -> bool:
        return self.net_profit > 0


def top_of_book_edge(
    yes_ask_a: float | None,
    no_ask_b: float | None,
    fee_a: FeeModel,
    fee_b: FeeModel,
    contracts: float = 1.0,
) -> float | None:
    """Net profit per `contracts` for buying YES@A + NO@B at top of book.

    Returns None if either side is unpriced. May be negative (no edge).
    """
    if yes_ask_a is None or no_ask_b is None:
        return None
    cost = (yes_ask_a + no_ask_b) * contracts
    fees = fee_a.trade_fee(contracts, yes_ask_a) + fee_b.trade_fee(contracts, no_ask_b)
    return contracts - cost - fees


def _walk(asks_a: OutcomeBook, asks_b: OutcomeBook) -> tuple[float, float, float]:
    """Consume both ask ladders in lockstep while the combined price < $1.

    Returns (contracts, cost_a, cost_b). Ladders are best-first (ascending), so
    the combined marginal price is non-decreasing — we stop at the first level
    pair with no gross edge.
    """
    la, lb = asks_a.asks, asks_b.asks
    i = j = 0
    rem_a = la[0].size if la else 0.0
    rem_b = lb[0].size if lb else 0.0
    contracts = cost_a = cost_b = 0.0
    while i < len(la) and j < len(lb):
        pa, pb = la[i].price, lb[j].price
        if pa + pb >= 1.0:
            break
        take = min(rem_a, rem_b)
        contracts += take
        cost_a += pa * take
        cost_b += pb * take
        rem_a -= take
        rem_b -= take
        if rem_a <= 0:
            i += 1
            rem_a = la[i].size if i < len(la) else 0.0
        if rem_b <= 0:
            j += 1
            rem_b = lb[j].size if j < len(lb) else 0.0
    return contracts, cost_a, cost_b


def _direction(
    book_a: MarketBook,
    book_b: MarketBook,
    a_outcome: Outcome,
    b_outcome: Outcome,
    fee_a: FeeModel,
    fee_b: FeeModel,
) -> ArbOpportunity | None:
    contracts, cost_a, cost_b = _walk(book_a.book_for(a_outcome), book_b.book_for(b_outcome))
    if contracts <= 0:
        return None
    avg_a, avg_b = cost_a / contracts, cost_b / contracts
    fee_a_total = fee_a.trade_fee(contracts, avg_a)
    fee_b_total = fee_b.trade_fee(contracts, avg_b)
    gross = contracts - cost_a - cost_b
    net = gross - fee_a_total - fee_b_total
    total_cost = cost_a + cost_b
    return ArbOpportunity(
        contracts=contracts,
        gross_profit=gross,
        net_profit=net,
        roi=net / total_cost if total_cost else 0.0,
        net_edge_per_contract=net / contracts,
        legs=(
            ArbLeg(book_a.venue, a_outcome, contracts, avg_a, cost_a, fee_a_total),
            ArbLeg(book_b.venue, b_outcome, contracts, avg_b, cost_b, fee_b_total),
        ),
    )


def find_arbitrage(
    book_a: MarketBook,
    book_b: MarketBook,
    fee_a: FeeModel,
    fee_b: FeeModel,
    min_net_profit: float = 0.0,
) -> ArbOpportunity | None:
    """Best fee-net arbitrage across both directions, or None if none clears
    `min_net_profit`."""
    candidates = [
        _direction(book_a, book_b, Outcome.YES, Outcome.NO, fee_a, fee_b),
        _direction(book_a, book_b, Outcome.NO, Outcome.YES, fee_a, fee_b),
    ]
    best = max(
        (c for c in candidates if c is not None),
        key=lambda o: o.net_profit,
        default=None,
    )
    if best is None or best.net_profit <= min_net_profit:
        return None
    return best
