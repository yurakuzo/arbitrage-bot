"""Deterministic tests for the fee models — these gate whether edge is real."""

from __future__ import annotations

import math

import pytest

from arb.core.fees import KalshiFeeModel, PolymarketFeeModel


def test_kalshi_fee_peaks_at_midpoint():
    m = KalshiFeeModel()
    # Verified: 0.07 * 100 * 0.5 * 0.5 = 1.75 -> $1.75 per 100 contracts at P=0.50.
    assert m.trade_fee(100, 0.50) == pytest.approx(1.75)


def test_kalshi_fee_is_convex_lower_at_tails():
    m = KalshiFeeModel()
    mid = m.trade_fee(100, 0.50)
    tail = m.trade_fee(100, 0.10)
    assert tail < mid


def test_kalshi_fee_rounds_up_to_cent():
    m = KalshiFeeModel()
    fee = m.trade_fee(1, 0.50)  # 0.07*1*0.25 = 0.0175 -> ceil to 0.02
    assert fee == pytest.approx(0.02)
    # never fractional cents
    assert math.isclose(round(fee * 100), fee * 100)


def test_kalshi_maker_rate_is_cheaper():
    m = KalshiFeeModel()
    assert m.trade_fee(100, 0.50, maker=True) < m.trade_fee(100, 0.50, maker=False)


def test_polymarket_default_is_zero():
    m = PolymarketFeeModel()
    assert m.trade_fee(100, 0.50) == 0.0


def test_polymarket_bps_scales_with_notional():
    m = PolymarketFeeModel(taker_bps=50)  # 0.5%
    # notional = 100 * 0.40 = 40 ; fee = 40 * 0.005 = 0.20
    assert m.trade_fee(100, 0.40) == pytest.approx(0.20)
