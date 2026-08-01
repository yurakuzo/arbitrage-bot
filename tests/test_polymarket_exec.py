"""Phase 4b tests: Polymarket credential loading, order-arg mapping, gate block.

No network and no py-clob-client import — the live submit path is behind the gate
and a lazy import, so we test everything up to (not including) the real call.
"""

from __future__ import annotations

import pytest

from arb.providers.auth.polymarket_auth import PolymarketCredentials
from arb.providers.models import Order, Outcome, Side, Venue
from arb.providers.polymarket import PolymarketProvider, build_clob_order_args


def _wallet_file(tmp_path, **extra):
    lines = [
        "PRIVATE_KEY=0xabc123deadbeef",
        "PROXY_WALLET=0xProxyWallet",
        "USER_ADDRESS=0xEOA",
        "CLOB_HTTP_URL=https://clob.polymarket.com",
    ]
    lines += [f"{k}={v}" for k, v in extra.items()]
    f = tmp_path / ".env.testwallet"
    f.write_text("\n".join(lines), encoding="utf-8")
    return f


def test_credentials_from_env_file(tmp_path):
    creds = PolymarketCredentials.from_env_file(_wallet_file(tmp_path), default_signature_type=2)
    assert creds.private_key == "0xabc123deadbeef"
    assert creds.proxy_wallet == "0xProxyWallet"
    assert creds.signature_type == 2
    assert creds.host == "https://clob.polymarket.com"
    assert creds.chain_id == 137


def test_credentials_signature_type_override(tmp_path):
    creds = PolymarketCredentials.from_env_file(_wallet_file(tmp_path, SIGNATURE_TYPE="1"))
    assert creds.signature_type == 1


def test_credentials_repr_redacts_key(tmp_path):
    creds = PolymarketCredentials.from_env_file(_wallet_file(tmp_path))
    text = repr(creds)
    assert "0xabc123deadbeef" not in text
    assert "redacted" in text


def test_credentials_missing_key(tmp_path):
    f = tmp_path / ".env.bad"
    f.write_text("PROXY_WALLET=0xonly", encoding="utf-8")
    with pytest.raises(ValueError, match="PRIVATE_KEY"):
        PolymarketCredentials.from_env_file(f)


def test_credentials_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        PolymarketCredentials.from_env_file(tmp_path / "nope.env")


def test_build_clob_order_args():
    o = Order(Venue.POLYMARKET, "0xcond", Outcome.YES, Side.BUY, 25, 0.436)
    args = build_clob_order_args(o, token_id="TOKEN-YES")
    assert args == {"token_id": "TOKEN-YES", "price": 0.436, "size": 25.0, "side": "BUY"}


@pytest.mark.asyncio
async def test_place_order_blocked_by_gate_before_any_client():
    from arb.live.gate import TradingBlocked, TradingGate

    # Gate closed (paper/demo) -> must raise before resolving tokens or building a client.
    gate = TradingGate(live_trading=False, mode="paper", environment="demo", max_order_stake_usd=50)
    prov = PolymarketProvider(wallet_env_file=None)  # no creds on purpose
    order = Order(Venue.POLYMARKET, "0xcond", Outcome.YES, Side.BUY, 10, 0.40)
    with pytest.raises(TradingBlocked):
        await prov.place_order(order, gate)
