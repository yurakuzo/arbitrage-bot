"""Tests for the live engine: canonicalization, evaluation, and the paper loop."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from arb.config import ThresholdConfig
from arb.core.matching import CanonicalPair, OutcomeMapping
from arb.infra.anomaly import AnomalyReporter
from arb.infra.db import Database
from arb.infra.telegram import TelegramNotifier
from arb.live.engine import Engine, canonicalize, evaluate
from arb.live.simulator import PaperLedger
from arb.providers.models import MarketBook, Outcome, OutcomeBook, PriceLevel, Venue


def _book(venue, market_id, *, yes_asks=(), no_asks=(), age_s=0.0):
    ts = datetime.now(UTC) - timedelta(seconds=age_s)
    return MarketBook(
        venue=venue,
        market_id=market_id,
        ts=ts,
        yes=OutcomeBook(asks=[PriceLevel(p, s) for p, s in yes_asks]),
        no=OutcomeBook(asks=[PriceLevel(p, s) for p, s in no_asks]),
    )


def _pair(k_out=Outcome.YES, p_out=Outcome.YES):
    return CanonicalPair(
        canonical_id="test-pair",
        description="",
        legs={
            "kalshi": OutcomeMapping("kalshi", "KX-1", k_out),
            "polymarket": OutcomeMapping("polymarket", "0xabc", p_out),
        },
    )


def test_canonicalize_swaps_on_no():
    b = _book(Venue.KALSHI, "KX-1", yes_asks=[(0.4, 10)], no_asks=[(0.6, 20)])
    same = canonicalize(b, Outcome.YES)
    swapped = canonicalize(b, Outcome.NO)
    assert same.yes.best_ask == 0.4
    assert swapped.yes.best_ask == 0.6  # yes/no flipped


def test_evaluate_detects_arb():
    th = ThresholdConfig()
    ka = _book(Venue.KALSHI, "KX-1", yes_asks=[(0.40, 100)])
    pm = _book(Venue.POLYMARKET, "0xabc", no_asks=[(0.55, 60)])
    res = evaluate(_pair(), ka, pm, th, datetime.now(UTC))
    assert res.status == "arb"
    assert res.opp is not None and res.opp.net_profit > 0
    assert res.opp.contracts == 60


def test_evaluate_stale_quote():
    th = ThresholdConfig(max_quote_age_ms=1000)
    ka = _book(Venue.KALSHI, "KX-1", yes_asks=[(0.40, 100)], age_s=5)  # 5s > 1s
    pm = _book(Venue.POLYMARKET, "0xabc", no_asks=[(0.55, 60)])
    res = evaluate(_pair(), ka, pm, th, datetime.now(UTC))
    assert res.status == "stale"


def test_evaluate_no_arb_when_sum_over_one():
    th = ThresholdConfig()
    ka = _book(Venue.KALSHI, "KX-1", yes_asks=[(0.60, 100)])
    pm = _book(Venue.POLYMARKET, "0xabc", no_asks=[(0.60, 60)])
    res = evaluate(_pair(), ka, pm, th, datetime.now(UTC))
    assert res.status == "no_arb"


def test_evaluate_stake_cap_scales_size():
    th = ThresholdConfig(max_stake_usd=10.0)  # tiny cap
    ka = _book(Venue.KALSHI, "KX-1", yes_asks=[(0.40, 100)])
    pm = _book(Venue.POLYMARKET, "0xabc", no_asks=[(0.55, 60)])
    res = evaluate(_pair(), ka, pm, th, datetime.now(UTC))
    assert res.status == "arb"
    total_cost = sum(lg.cost for lg in res.opp.legs)
    assert total_cost <= 10.0 + 1e-6  # capped


def _engine(db):
    return Engine(
        providers={},
        pairs=[_pair()],
        ledger=PaperLedger(db=db),
        anomaly=AnomalyReporter(TelegramNotifier(None, None)),  # disabled -> no network
        thresholds=ThresholdConfig(),
    )


@pytest.mark.asyncio
async def test_engine_records_once_with_hysteresis(tmp_path):
    db = Database(tmp_path / "e.sqlite")
    db.init_schema()
    eng = _engine(db)

    ka = _book(Venue.KALSHI, "KX-1", yes_asks=[(0.40, 100)])
    pm = _book(Venue.POLYMARKET, "0xabc", no_asks=[(0.55, 60)])

    await eng._on_book(ka)
    await eng._on_book(pm)  # both legs present -> arb -> 1 trade
    assert eng.ledger.trades == 1

    # Repeated update while opportunity stays open -> no duplicate fill.
    await eng._on_book(_book(Venue.POLYMARKET, "0xabc", no_asks=[(0.55, 60)]))
    assert eng.ledger.trades == 1

    # Opportunity disappears (no edge) -> hysteresis resets.
    await eng._on_book(_book(Venue.POLYMARKET, "0xabc", no_asks=[(0.90, 60)]))
    assert eng.ledger.trades == 1

    # New opportunity -> fires again.
    await eng._on_book(_book(Venue.POLYMARKET, "0xabc", no_asks=[(0.55, 60)]))
    assert eng.ledger.trades == 2
