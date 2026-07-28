"""Phase 4 tests: the safety gate, Kalshi signing, order building, executor routing.

These deliberately assert that NOTHING places a real order unless every gate
condition holds, and that outcome translation is correct.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from arb.config import ExecutionConfig, Settings
from arb.core.matching import CanonicalPair, OutcomeMapping
from arb.core.pricing import ArbLeg, ArbOpportunity
from arb.infra.anomaly import AnomalyReporter
from arb.infra.db import Database
from arb.infra.telegram import TelegramNotifier
from arb.live.executor import Executor, build_orders, native_outcome
from arb.live.gate import TradingBlocked, TradingGate
from arb.live.simulator import PaperLedger
from arb.providers.kalshi import build_order_payload
from arb.providers.models import Order, Outcome, Side, Venue


# --- Gate: the safety-critical logic --------------------------------------------
def _gate(live=False, mode="paper", env="demo", cap=50.0):
    return TradingGate(live_trading=live, mode=mode, environment=env, max_order_stake_usd=cap)


def test_gate_default_is_paper():
    assert _gate().places_real_orders is False


def test_gate_requires_all_conditions():
    assert _gate(live=True, mode="auto", env="prod").places_real_orders is True
    # Any single missing condition -> no real orders.
    assert _gate(live=False, mode="auto", env="prod").places_real_orders is False
    assert _gate(live=True, mode="paper", env="prod").places_real_orders is False
    assert _gate(live=True, mode="auto", env="demo").places_real_orders is False


@pytest.mark.parametrize(
    "kwargs",
    [
        {"live": False, "mode": "auto", "env": "prod"},
        {"live": True, "mode": "paper", "env": "prod"},
        {"live": True, "mode": "auto", "env": "demo"},
    ],
)
def test_check_order_blocks_unless_fully_open(kwargs):
    with pytest.raises(TradingBlocked):
        _gate(**kwargs).check_order(10.0)


def test_check_order_enforces_stake_cap():
    g = _gate(live=True, mode="auto", env="prod", cap=50.0)
    g.check_order(50.0)  # ok
    with pytest.raises(TradingBlocked):
        g.check_order(50.01)


def test_gate_from_config_defaults_safe():
    g = TradingGate.from_config(Settings(environment="demo"), ExecutionConfig())
    assert g.places_real_orders is False


# --- Kalshi signing + payload ---------------------------------------------------
def test_kalshi_signer_produces_verifiable_signature():
    crypto = pytest.importorskip("cryptography")
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa

    from arb.providers.auth.kalshi_auth import KalshiSigner

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    signer = KalshiSigner("kid-1", pem)
    headers = signer.headers("POST", "/trade-api/v2/portfolio/orders", timestamp_ms=1730000000000)
    assert headers["KALSHI-ACCESS-KEY"] == "kid-1"
    import base64

    sig = base64.b64decode(headers["KALSHI-ACCESS-SIGNATURE"])
    msg = (headers["KALSHI-ACCESS-TIMESTAMP"] + "POST" + "/trade-api/v2/portfolio/orders").encode()
    # Verifies against the public key -> signature is correct.
    key.public_key().verify(
        sig, msg,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    _ = crypto


def test_kalshi_order_payload_prices_in_cents():
    o = Order(Venue.KALSHI, "KXT-1", Outcome.YES, Side.BUY, 10, 0.37)
    body = build_order_payload(o)
    assert body["ticker"] == "KXT-1"
    assert body["action"] == "buy" and body["side"] == "yes"
    assert body["yes_price"] == 37 and body["count"] == 10
    no = build_order_payload(Order(Venue.KALSHI, "KXT-1", Outcome.NO, Side.BUY, 5, 0.62))
    assert no["no_price"] == 62 and "yes_price" not in no


# --- Outcome translation --------------------------------------------------------
def test_native_outcome_translation():
    # Event side YES -> canonical YES stays YES; canonical NO -> NO.
    assert native_outcome(Outcome.YES, Outcome.YES) is Outcome.YES
    assert native_outcome(Outcome.NO, Outcome.YES) is Outcome.NO
    # Opposite framing: mapping says the venue's NO == event.
    assert native_outcome(Outcome.YES, Outcome.NO) is Outcome.NO
    assert native_outcome(Outcome.NO, Outcome.NO) is Outcome.YES


def _opp():
    return ArbOpportunity(
        contracts=60, gross_profit=3.0, net_profit=2.0, roi=0.03, net_edge_per_contract=0.033,
        legs=(
            ArbLeg(Venue.KALSHI, Outcome.YES, 60, 0.40, 24.0, 1.0),
            ArbLeg(Venue.POLYMARKET, Outcome.NO, 60, 0.55, 33.0, 0.0),
        ),
    )


def _pair(k_out=Outcome.YES, p_out=Outcome.YES):
    return CanonicalPair(
        "p", "d",
        {"kalshi": OutcomeMapping("kalshi", "KX-1", k_out),
         "polymarket": OutcomeMapping("polymarket", "0xabc", p_out)},
    )


def test_build_orders_uses_native_sides_on_opposite_framing():
    # polymarket leg mapped as NO-event -> canonical NO becomes native YES.
    orders = build_orders(_pair(p_out=Outcome.NO), _opp())
    by_venue = {o.venue.value: o for o in orders}
    assert by_venue["kalshi"].outcome is Outcome.YES
    assert by_venue["polymarket"].outcome is Outcome.YES  # translated from canonical NO


# --- Executor routing -----------------------------------------------------------
def _executor(db, mode="paper", gate=None):
    notifier = TelegramNotifier(None, None)
    return Executor(
        mode=mode,
        providers={},
        notifier=notifier,
        ledger=PaperLedger(db=db),
        gate=gate or _gate(),
        execution=ExecutionConfig(mode=mode),
        anomaly=AnomalyReporter(notifier),
    )


@pytest.mark.asyncio
async def test_executor_paper_records_no_real_order(tmp_path):
    db = Database(tmp_path / "x.sqlite"); db.init_schema()
    ex = _executor(db, mode="paper")
    placed = await ex.execute(_pair(), _opp(), datetime.now(UTC))
    assert placed is False  # simulated only
    assert ex.ledger.trades == 1


@pytest.mark.asyncio
async def test_executor_falls_back_to_paper_when_gate_closed(tmp_path):
    db = Database(tmp_path / "y.sqlite"); db.init_schema()
    # mode=auto but gate closed (demo/not-live) -> must NOT place, records paper.
    ex = _executor(db, mode="auto", gate=_gate(live=True, mode="auto", env="demo"))
    placed = await ex.execute(_pair(), _opp(), datetime.now(UTC))
    assert placed is False
    assert ex.ledger.trades == 1
