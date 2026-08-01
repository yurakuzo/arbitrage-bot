"""Kalshi provider (analysis mode: public REST discovery + order books).

Verified response shapes (2026-07):
  GET /markets            -> {"cursor": str, "markets": [ {...}, ... ]}
  GET /markets/{t}/orderbook -> {"orderbook_fp": {"yes_dollars": [[p,s],...],
                                                   "no_dollars":  [[p,s],...]}}
Prices in the *_dollars fields are already dollars in [0,1] (strings).

Kalshi's order book only lists resting BIDS on each side. To BUY an outcome you
cross the *other* side's bids, so the ask ladders are derived:
  yes ask @ (1 - no_bid_price)   (size = that no-bid's size)
  no  ask @ (1 - yes_bid_price)

Public market data works against the production host without auth; RSA-signed
auth is only needed for account/order endpoints (Phase 4).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from arb.core.fees import FeeModel, KalshiFeeModel
from arb.infra.logging import get_logger
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

PROD_BASE = "https://api.elections.kalshi.com/trade-api/v2"
DEMO_BASE = "https://demo-api.kalshi.co/trade-api/v2"

log = get_logger(__name__)


def _is_noise(m: Market) -> bool:
    """Auto-generated markets that flood the listing and aren't arb-viable:
    provisional markets and multivariate sports parlays (KXMVE...)."""
    if m.raw.get("is_provisional"):
        return True
    return (m.raw.get("event_ticker") or "").startswith("KXMVE")


def _f(value: str | float | None) -> float | None:
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


def _series_key(event_ticker: str | None) -> str | None:
    # Kalshi series ticker is the leading segment of the event ticker,
    # e.g. "KXHIGHLON-26JUL27" -> "KXHIGHLON". Groups recurring markets.
    if not event_ticker:
        return None
    return event_ticker.split("-", 1)[0]


def parse_market(raw: dict) -> Market:
    return Market(
        venue=Venue.KALSHI,
        market_id=raw["ticker"],
        title=raw.get("title") or raw.get("yes_sub_title") or raw["ticker"],
        close_time=_parse_dt(raw.get("close_time")),
        series_key=_series_key(raw.get("event_ticker")),
        raw={
            "event_ticker": raw.get("event_ticker"),
            "status": raw.get("status"),
            "is_provisional": raw.get("is_provisional"),
            "volume": _f(raw.get("volume_fp")),
            "volume_24h": _f(raw.get("volume_24h_fp")),
            "liquidity": _f(raw.get("liquidity_dollars")),
        },
    )


def _levels(rows: list | None, *, invert: bool) -> list[PriceLevel]:
    """Build a ladder from Kalshi [[price, size], ...] bid rows.

    invert=True converts opposite-side bids into this side's ask ladder
    (price -> 1 - price). Result is sorted best-first (asks ascending,
    bids descending).
    """
    out: list[PriceLevel] = []
    for row in rows or []:
        p = _f(row[0])
        s = _f(row[1])
        if p is None or s is None:
            continue
        # Round the derived ask to kill 1-p binary FP noise (prices are cents).
        out.append(PriceLevel(price=round(1.0 - p, 4) if invert else p, size=s))
    out.sort(key=lambda lv: lv.price, reverse=not invert)
    return out


def parse_order_book(market_id: str, payload: dict, ts: datetime) -> MarketBook:
    ob = payload.get("orderbook_fp") or payload.get("orderbook") or {}
    yes_bids_raw = ob.get("yes_dollars") or ob.get("yes")
    no_bids_raw = ob.get("no_dollars") or ob.get("no")
    return MarketBook(
        venue=Venue.KALSHI,
        market_id=market_id,
        ts=ts,
        yes=OutcomeBook(
            bids=_levels(yes_bids_raw, invert=False),
            asks=_levels(no_bids_raw, invert=True),  # buy YES by crossing NO bids
        ),
        no=OutcomeBook(
            bids=_levels(no_bids_raw, invert=False),
            asks=_levels(yes_bids_raw, invert=True),  # buy NO by crossing YES bids
        ),
    )


def build_order_payload(order: Order) -> dict:
    """Translate a normalized Order into a Kalshi create-order body (pure).

    Kalshi prices are integer cents (1-99); a BUY sets the price on the chosen
    side. IOC/FOK map to Kalshi's immediate execution; GTC rests.
    """
    price_cents = max(1, min(99, round(order.limit_price * 100)))
    body = {
        "ticker": order.market_id,
        "action": order.side.value,  # "buy" | "sell"
        "side": order.outcome.value,  # "yes" | "no"
        "type": "limit",
        "count": int(order.contracts),
        "client_order_id": order.client_order_id or str(uuid.uuid4()),
    }
    if order.outcome is Outcome.YES:
        body["yes_price"] = price_cents
    else:
        body["no_price"] = price_cents
    return body


class KalshiProvider(Provider):
    venue = Venue.KALSHI

    _ORDERS_PATH = "/trade-api/v2/portfolio/orders"

    def __init__(
        self,
        environment: str = "demo",
        key_id: str | None = None,
        private_key_path: str | None = None,
    ):
        # Public read endpoints are served by the production host; authenticated
        # order calls require RSA credentials (Phase 4).
        self.environment = environment
        self._http = RestClient(base_url=PROD_BASE)
        self._key_id = key_id
        self._private_key_path = private_key_path
        self._signer = None  # built lazily on first authenticated call

    def _get_signer(self):
        if self._signer is None:
            if not (self._key_id and self._private_key_path):
                raise RuntimeError(
                    "Kalshi live trading needs ARB_KALSHI_API_KEY_ID and "
                    "ARB_KALSHI_PRIVATE_KEY_PATH."
                )
            from arb.providers.auth.kalshi_auth import KalshiSigner

            self._signer = KalshiSigner.from_file(self._key_id, self._private_key_path)
        return self._signer

    async def place_order(self, order: Order, gate: TradingGate) -> OrderResult:
        # Gate first — the only path to a real order, and it must be fully open.
        stake = order.contracts * order.limit_price
        gate.check_order(stake)  # raises TradingBlocked unless permitted
        if order.side is not Side.BUY:
            raise NotImplementedError("Only BUY legs are supported in v1 arbitrage.")

        signer = self._get_signer()
        payload = build_order_payload(order)
        headers = signer.headers("POST", self._ORDERS_PATH)
        import httpx  # local import; only needed for the live path

        url = f"{PROD_BASE}/portfolio/orders"
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(url, json=payload, headers=headers)
            ok = resp.is_success
            data = resp.json() if resp.content else {}
        o = data.get("order", data) if isinstance(data, dict) else {}
        return OrderResult(
            ok=ok,
            order_id=o.get("order_id"),
            status=o.get("status", "" if ok else f"http_{resp.status_code}"),
            raw=data if isinstance(data, dict) else {},
        )

    async def list_markets(self, flt: MarketFilter) -> list[Market]:
        target = flt.limit or 500
        markets: list[Market] = []
        cursor: str | None = None
        while len(markets) < target:
            params: dict = {"limit": min(1000, target - len(markets))}
            if flt.status:
                params["status"] = flt.status
            if flt.series_key:
                params["series_ticker"] = flt.series_key
            if cursor:
                params["cursor"] = cursor
            data = await self._http.get_json("/markets", params=params)
            batch = data.get("markets", []) if isinstance(data, dict) else []
            if not batch:
                break
            markets.extend(parse_market(m) for m in batch)
            cursor = data.get("cursor") if isinstance(data, dict) else None
            if not cursor:
                break
        return markets[:target]

    async def _sweep_events(self, max_markets: int) -> list[Market]:
        """Paginate the /events stream and collect real (non-noise) markets.

        /events is diverse and NOT dominated by the auto-generated sports parlays
        that flood /markets, so this is the efficient bulk source. (Kalshi ignores
        the ?category= filter here, so we sweep the whole stream and rank later.)
        """
        out: list[Market] = []
        cursor: str | None = None
        while len(out) < max_markets:
            params: dict = {"status": "open", "with_nested_markets": "true", "limit": 200}
            if cursor:
                params["cursor"] = cursor
            data = await self._http.get_json("/events", params=params)
            events = data.get("events", []) if isinstance(data, dict) else []
            if not events:
                break
            for ev in events:
                ev_ticker = ev.get("event_ticker")
                for m in ev.get("markets", []) or []:
                    # Nested markets may omit event_ticker; inject so series_key resolves.
                    m.setdefault("event_ticker", ev_ticker)
                    market = parse_market(m)
                    if not _is_noise(market):
                        out.append(market)
            cursor = data.get("cursor") if isinstance(data, dict) else None
            if not cursor:
                break
        return out[:max_markets]

    async def discover(self, limit: int, discovery: dict | None = None) -> list[Market]:
        """Discover real markets: a bulk /events sweep plus any explicit series,
        dropping provisional/MVE sports noise. Kalshi's flat /markets listing is
        >90% auto-generated parlays, so we never sweep it blindly."""
        discovery = discovery or {}
        out: list[Market] = []

        # Explicit series (each expands to its markets).
        per = int(discovery.get("per_series_limit") or 100)
        for st in discovery.get("series") or []:
            try:
                ms = await self.list_markets(MarketFilter(series_key=st, status="open", limit=per))
                out.extend(m for m in ms if not _is_noise(m))
            except Exception as exc:  # noqa: BLE001
                log.warning("kalshi: series '%s' failed: %s", st, exc)

        # Bulk /events sweep (pull at least `limit` candidates to rank).
        cap = max(limit, int(discovery.get("max_markets") or 2000))
        if cap > 0:
            try:
                out.extend(await self._sweep_events(cap))
            except Exception as exc:  # noqa: BLE001
                log.warning("kalshi: /events sweep failed: %s", exc)

        seen: set[str] = set()
        deduped = [m for m in out if not (m.market_id in seen or seen.add(m.market_id))]
        log.info("kalshi: discovered %d unique markets", len(deduped))
        return deduped

    async def get_order_book(self, market_id: str) -> MarketBook:
        data = await self._http.get_json(f"/markets/{market_id}/orderbook")
        return parse_order_book(market_id, data, datetime.now(UTC))

    def fee_model(self, series_key: str | None = None) -> FeeModel:
        return KalshiFeeModel()

    async def aclose(self) -> None:
        await self._http.aclose()
