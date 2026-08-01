"""Tests for report URL/hyperlink building."""

from __future__ import annotations

import numpy as np

from arb.analysis.report import _market_url


def test_polymarket_url_from_event_slug():
    assert _market_url("polymarket", "0xabc", "2028-dem-nomination") == (
        "https://polymarket.com/event/2028-dem-nomination"
    )


def test_polymarket_url_none_without_event():
    assert _market_url("polymarket", "0xabc", None) is None
    assert _market_url("polymarket", "0xabc", np.nan) is None


def test_kalshi_url_uses_series():
    assert _market_url("kalshi", "KXHIGHNY-26AUG02-T84", "KXHIGHNY") == (
        "https://kalshi.com/markets/kxhighny"
    )


def test_kalshi_url_falls_back_to_ticker_prefix():
    # No series_key -> derive from the leading segment of the ticker.
    assert _market_url("kalshi", "KXHIGHNY-26AUG02-T84", np.nan) == (
        "https://kalshi.com/markets/kxhighny"
    )
