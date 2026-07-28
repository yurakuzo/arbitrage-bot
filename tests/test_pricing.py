"""Tests for cross-venue arbitrage pricing — the numbers that decide real edge."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from arb.core.fees import KalshiFeeModel, PolymarketFeeModel
from arb.core.pricing import find_arbitrage, top_of_book_edge
from arb.providers.models import MarketBook, OutcomeBook, PriceLevel, Venue

ZERO_FEE = PolymarketFeeModel()  # 0 bps


def _book(venue, yes_asks=(), no_asks=()):
    return MarketBook(
        venue=venue,
        market_id="m",
        ts=datetime.now(UTC),
        yes=OutcomeBook(asks=[PriceLevel(p, s) for p, s in yes_asks]),
        no=OutcomeBook(asks=[PriceLevel(p, s) for p, s in no_asks]),
    )


def test_top_of_book_edge_positive():
    # 0.40 + 0.55 = 0.95 -> 0.05 edge/contract, no fees.
    assert top_of_book_edge(0.40, 0.55, ZERO_FEE, ZERO_FEE) == pytest.approx(0.05)


def test_top_of_book_edge_negative_when_sum_over_one():
    assert top_of_book_edge(0.50, 0.55, ZERO_FEE, ZERO_FEE) == pytest.approx(-0.05)


def test_top_of_book_edge_none_when_unpriced():
    assert top_of_book_edge(None, 0.5, ZERO_FEE, ZERO_FEE) is None


def test_find_arbitrage_walks_depth_limited_size():
    a = _book(Venue.POLYMARKET, yes_asks=[(0.40, 100)])
    b = _book(Venue.KALSHI, no_asks=[(0.55, 60)])
    opp = find_arbitrage(a, b, ZERO_FEE, ZERO_FEE)
    assert opp is not None
    assert opp.contracts == 60  # limited by the thinner leg
    assert opp.net_profit == pytest.approx(3.0)  # 60 * (1 - 0.95)
    assert opp.net_edge_per_contract == pytest.approx(0.05)


def test_find_arbitrage_none_when_no_gross_edge():
    a = _book(Venue.POLYMARKET, yes_asks=[(0.60, 10)])
    b = _book(Venue.KALSHI, no_asks=[(0.60, 10)])  # 1.20 combined
    assert find_arbitrage(a, b, ZERO_FEE, ZERO_FEE) is None


def test_find_arbitrage_fees_can_eat_thin_edge():
    # Gross 0.02/contract over 100 contracts = $2, but Kalshi fees on both legs
    # near $0.50 exceed that -> no profitable arb.
    a = _book(Venue.KALSHI, yes_asks=[(0.49, 100)])
    b = _book(Venue.KALSHI, no_asks=[(0.49, 100)])
    fee = KalshiFeeModel()
    assert find_arbitrage(a, b, fee, fee) is None
