"""Polymarket provider (analysis mode: Gamma discovery + public CLOB books).

Verified response shapes (2026-07):
  Gamma GET /markets -> [ {id, question, conditionId, clobTokenIds: "[yes,no]",
                           outcomes, outcomePrices, volumeNum, liquidityNum,
                           bestBid, bestAsk, endDate, active, closed, ...}, ... ]
  CLOB  GET /book?token_id=<id> -> {market, asset_id, timestamp(ms),
                                    bids: [{price,size}], asks: [{price,size}],
                                    tick_size, min_order_size}

A binary market has two ERC-1155 tokens (YES then NO in clobTokenIds). Each token
has its own book, so we fetch both and combine. Public reads need no auth.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from arb.core.fees import FeeModel, PolymarketFeeModel
from arb.providers.base import Provider
from arb.providers.http import RestClient
from arb.providers.models import (
    Market,
    MarketBook,
    MarketFilter,
    Order,
    OrderResult,
    Outcome,
    OutcomeBook,
    PriceLevel,
    Side,
    Venue,
)

if TYPE_CHECKING:
    from arb.live.gate import TradingGate


def build_clob_order_args(order: Order, token_id: str) -> dict:
    """Pure mapping from a normalized Order to py-clob-client OrderArgs kwargs.

    Polymarket prices are dollars in [0,1] and size is in contracts (shares).
    """
    return {
        "token_id": token_id,
        "price": round(order.limit_price, 4),
        "size": float(order.contracts),
        "side": "BUY",
    }

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"


def _f(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)  # 3.11+ parses trailing 'Z'
    except ValueError:
        return None


def _token_ids(raw: dict) -> tuple[str, str] | None:
    """Return (yes_token_id, no_token_id) or None if unavailable."""
    tokens = raw.get("clobTokenIds")
    if isinstance(tokens, str):
        try:
            tokens = json.loads(tokens)
        except json.JSONDecodeError:
            return None
    if isinstance(tokens, list) and len(tokens) >= 2:
        return str(tokens[0]), str(tokens[1])
    return None


def parse_market(raw: dict) -> Market:
    return Market(
        venue=Venue.POLYMARKET,
        market_id=raw["conditionId"],
        title=raw.get("question") or raw.get("slug") or raw["conditionId"],
        close_time=_parse_dt(raw.get("endDate")),
        series_key=raw.get("groupItemTitle") or None,
        raw={
            "slug": raw.get("slug"),
            "token_ids": _token_ids(raw),
            "volume": _f(raw.get("volumeNum")) or _f(raw.get("volume")),
            "liquidity": _f(raw.get("liquidityNum")) or _f(raw.get("liquidity")),
            "best_bid": _f(raw.get("bestBid")),
            "best_ask": _f(raw.get("bestAsk")),
            "enable_order_book": raw.get("enableOrderBook"),
        },
    )


def parse_token_book(payload: dict) -> OutcomeBook:
    """Build an OutcomeBook from a single token's CLOB book payload."""

    def levels(rows, *, reverse):
        out = []
        for r in rows or []:
            p, s = _f(r.get("price")), _f(r.get("size"))
            if p is not None and s is not None:
                out.append(PriceLevel(price=p, size=s))
        out.sort(key=lambda lv: lv.price, reverse=reverse)
        return out

    return OutcomeBook(
        asks=levels(payload.get("asks"), reverse=False),  # cheapest first
        bids=levels(payload.get("bids"), reverse=True),  # highest first
    )


class PolymarketProvider(Provider):
    venue = Venue.POLYMARKET

    def __init__(
        self,
        environment: str = "demo",
        wallet_env_file: str | None = None,
        signature_type: int = 2,
    ):
        self.environment = environment
        self._gamma = RestClient(base_url=GAMMA_BASE)
        self._clob = RestClient(base_url=CLOB_BASE)
        # condition_id -> (yes_token, no_token); warmed by list_markets.
        self._token_cache: dict[str, tuple[str, str]] = {}
        # Live-execution creds are loaded lazily (only when placing an order).
        self._wallet_env_file = wallet_env_file
        self._signature_type = signature_type
        self._clob_client = None

    async def list_markets(self, flt: MarketFilter) -> list[Market]:
        target = flt.limit or 200
        params = {
            "active": "true",
            "closed": "false",
            "limit": min(500, target),
            "order": "volumeNum",
            "ascending": "false",
        }
        if flt.query:
            params["slug"] = flt.query
        data = await self._gamma.get_json("/markets", params=params)
        rows = data if isinstance(data, list) else data.get("data", [])
        markets: list[Market] = []
        for row in rows[:target]:
            if not row.get("conditionId") or not row.get("clobTokenIds"):
                continue
            m = parse_market(row)
            if m.raw.get("token_ids"):
                self._token_cache[m.market_id] = m.raw["token_ids"]
            markets.append(m)
        return markets

    async def _resolve_tokens(self, condition_id: str) -> tuple[str, str]:
        if condition_id in self._token_cache:
            return self._token_cache[condition_id]
        data = await self._gamma.get_json("/markets", params={"condition_ids": condition_id})
        rows = data if isinstance(data, list) else data.get("data", [])
        if rows:
            tokens = _token_ids(rows[0])
            if tokens:
                self._token_cache[condition_id] = tokens
                return tokens
        raise ValueError(f"Could not resolve CLOB token ids for market {condition_id}")

    async def get_order_book(self, market_id: str) -> MarketBook:
        yes_token, no_token = await self._resolve_tokens(market_id)
        yes_payload = await self._clob.get_json("/book", params={"token_id": yes_token})
        no_payload = await self._clob.get_json("/book", params={"token_id": no_token})
        ts_ms = _f(yes_payload.get("timestamp"))
        ts = (
            datetime.fromtimestamp(ts_ms / 1000, tz=UTC)
            if ts_ms
            else datetime.now(UTC)
        )
        return MarketBook(
            venue=Venue.POLYMARKET,
            market_id=market_id,
            ts=ts,
            yes=parse_token_book(yes_payload),
            no=parse_token_book(no_payload),
        )

    def fee_model(self, series_key: str | None = None) -> FeeModel:
        return PolymarketFeeModel()

    def _get_clob_client(self):
        """Build (and cache) an authenticated py-clob-client from the wallet env."""
        if self._clob_client is not None:
            return self._clob_client
        if not self._wallet_env_file:
            raise RuntimeError(
                "Polymarket live trading needs a wallet env file "
                "(ARB_POLYMARKET_ENV_FILE or `arb run --wallet-env <file>`)."
            )
        try:
            from py_clob_client.client import ClobClient
        except ImportError as exc:  # pragma: no cover - optional extra
            raise ImportError(
                "Polymarket live trading needs py-clob-client. Install with: "
                "pip install -e '.[live]'"
            ) from exc

        from arb.providers.auth.polymarket_auth import PolymarketCredentials

        creds = PolymarketCredentials.from_env_file(
            self._wallet_env_file, default_signature_type=self._signature_type
        )
        client = ClobClient(
            host=creds.host,
            key=creds.private_key,
            chain_id=creds.chain_id,
            signature_type=creds.signature_type,
            funder=creds.proxy_wallet,
        )
        # Derive L2 API credentials from the wallet (L1 EIP-712 signature).
        client.set_api_creds(client.create_or_derive_api_creds())
        self._clob_client = client
        return client

    async def place_order(self, order: Order, gate: TradingGate) -> OrderResult:
        # Gate first — the only path to a real order, and it must be fully open.
        gate.check_order(order.contracts * order.limit_price)
        if order.side is not Side.BUY:
            raise NotImplementedError("Only BUY legs are supported in v1 arbitrage.")

        yes_token, no_token = await self._resolve_tokens(order.market_id)
        token_id = yes_token if order.outcome is Outcome.YES else no_token
        args = build_clob_order_args(order, token_id)
        client = self._get_clob_client()

        def _submit() -> dict:
            # py-clob-client is synchronous; run off the event loop.
            from py_clob_client.clob_types import OrderArgs, OrderType
            from py_clob_client.order_builder.constants import BUY

            signed = client.create_order(
                OrderArgs(token_id=args["token_id"], price=args["price"],
                          size=args["size"], side=BUY)
            )
            return client.post_order(signed, OrderType.FOK)

        resp = await asyncio.to_thread(_submit)
        resp = resp if isinstance(resp, dict) else {}
        ok = bool(resp.get("success", resp.get("status") in ("matched", "live", "success")))
        return OrderResult(
            ok=ok,
            order_id=resp.get("orderID") or resp.get("orderId") or resp.get("id"),
            status=str(resp.get("status", "")),
            raw=resp,
        )

    async def aclose(self) -> None:
        await self._gamma.aclose()
        await self._clob.aclose()
