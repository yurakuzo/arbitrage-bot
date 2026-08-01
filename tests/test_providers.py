"""Parser tests using real payload shapes captured from the live APIs."""

from __future__ import annotations

from datetime import UTC, datetime

from arb.providers import kalshi, polymarket
from arb.providers.models import Venue


def test_kalshi_parse_market():
    raw = {
        "ticker": "KXHIGHLON-26JUL27-B",
        "title": "High temp in London",
        "event_ticker": "KXHIGHLON-26JUL27",
        "close_time": "2026-07-27T23:45:00Z",
        "status": "active",
        "volume_fp": "1234.0",
        "liquidity_dollars": "500.0",
    }
    m = kalshi.parse_market(raw)
    assert m.venue is Venue.KALSHI
    assert m.market_id == "KXHIGHLON-26JUL27-B"
    assert m.series_key == "KXHIGHLON"  # leading segment of event ticker
    assert m.close_time.tzinfo is not None
    assert m.raw["volume"] == 1234.0


def test_kalshi_orderbook_derives_asks_from_opposite_bids():
    payload = {
        "orderbook_fp": {
            "no_dollars": [["0.8730", "6000.00"], ["0.9150", "69000.00"]],
            "yes_dollars": [],
        }
    }
    book = kalshi.parse_order_book("KXTEST", payload, datetime.now(UTC))
    # NO bids sorted best (highest) first.
    assert book.no.best_bid == 0.9150
    # YES asks derived from NO bids: 1 - 0.9150 = 0.085 is the cheapest way to buy YES.
    assert book.yes.best_ask == round(1 - 0.9150, 4)
    assert book.yes.asks[0].size == 69000.0
    # No YES bids -> no derived NO asks.
    assert book.no.asks == []
    assert book.yes.bids == []


def test_polymarket_parse_market_and_tokens():
    raw = {
        "conditionId": "0xabc",
        "question": "Will Oprah Winfrey win the 2028 Democratic nomination?",
        "slug": "oprah-2028-dem",
        "endDate": "2026-07-31T12:00:00Z",
        "clobTokenIds": '["111", "222"]',
        "volumeNum": 877540.22,
        "liquidityNum": 14104.15,
        "bestBid": "0.50",
        "bestAsk": "0.51",
        "groupItemTitle": "Oprah Winfrey",
        "events": [{"ticker": "2028-dem-nomination", "slug": "2028-dem-nomination",
                    "title": "2028 Democratic nomination"}],
    }
    m = polymarket.parse_market(raw)
    assert m.market_id == "0xabc"
    assert m.raw["token_ids"] == ("111", "222")  # (YES, NO)
    assert m.raw["volume"] == 877540.22
    # series_key groups by the parent EVENT, not the per-candidate label.
    assert m.series_key == "2028-dem-nomination"
    assert m.raw["outcome_label"] == "Oprah Winfrey"


def test_polymarket_series_key_falls_back_and_handles_missing():
    # No events -> None (not the outcome label).
    m = polymarket.parse_market(
        {"conditionId": "0x1", "question": "q", "clobTokenIds": '["1","2"]',
         "groupItemTitle": "Some Candidate"}
    )
    assert m.series_key is None
    # ticker missing -> falls back to slug.
    m2 = polymarket.parse_market(
        {"conditionId": "0x2", "question": "q", "clobTokenIds": '["1","2"]',
         "events": [{"slug": "event-slug"}]}
    )
    assert m2.series_key == "event-slug"


def test_kalshi_noise_filter():
    real = kalshi.parse_market(
        {"ticker": "KXHIGHNY-26JUL28-T84", "title": "High temp NYC",
         "event_ticker": "KXHIGHNY-26JUL28", "volume_fp": "1435.0"}
    )
    provisional = kalshi.parse_market(
        {"ticker": "X", "title": "p", "event_ticker": "KXFOO", "is_provisional": True}
    )
    parlay = kalshi.parse_market(
        {"ticker": "Y", "title": "m", "event_ticker": "KXMVESPORTSMULTIGAMEEXTENDED-S1"}
    )
    assert kalshi._is_noise(real) is False
    assert kalshi._is_noise(provisional) is True
    assert kalshi._is_noise(parlay) is True


def test_polymarket_token_book_sorting():
    payload = {
        "bids": [{"price": "0.001", "size": "100"}, {"price": "0.019", "size": "50"}],
        "asks": [{"price": "0.999", "size": "10"}, {"price": "0.02", "size": "200"}],
    }
    ob = polymarket.parse_token_book(payload)
    assert ob.best_ask == 0.02  # cheapest ask first
    assert ob.best_bid == 0.019  # highest bid first
    assert ob.asks[0].size == 200.0
