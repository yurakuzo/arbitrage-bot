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

from datetime import UTC, datetime

from arb.core.fees import FeeModel, KalshiFeeModel
from arb.infra.logging import get_logger
from arb.providers.base import Provider
from arb.providers.http import RestClient
from arb.providers.models import (
    Market,
    MarketBook,
    MarketFilter,
    OutcomeBook,
    PriceLevel,
    Venue,
)

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


class KalshiProvider(Provider):
    venue = Venue.KALSHI

    def __init__(self, environment: str = "demo"):
        # Public read endpoints are served by the production host; account/order
        # calls (Phase 4) will honor the demo/prod switch.
        self.environment = environment
        self._http = RestClient(base_url=PROD_BASE)

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

    async def _series_in_category(self, category: str) -> list[str]:
        data = await self._http.get_json("/series", params={"category": category})
        series = data.get("series", []) if isinstance(data, dict) else []
        return [s["ticker"] for s in series if s.get("ticker")]

    async def discover(self, limit: int, discovery: dict | None = None) -> list[Market]:
        """Sweep curated series (and/or whole categories); drop provisional/MVE noise.

        Kalshi's default /markets listing is >90% auto-generated sports parlays,
        so blind top-N discovery is useless. Series/category sweeps return the
        real recurring markets (weather, econ, politics).
        """
        discovery = discovery or {}
        series: list[str] = list(discovery.get("series") or [])
        for cat in discovery.get("categories") or []:
            try:
                series += await self._series_in_category(cat)
            except Exception as exc:  # noqa: BLE001
                log.warning("kalshi: category '%s' lookup failed: %s", cat, exc)

        # De-duplicate, preserve order.
        seen: set[str] = set()
        series = [s for s in series if not (s in seen or seen.add(s))]

        if not series:
            log.warning("kalshi: no series/categories configured; falling back to raw listing")
            pool = await self.list_markets(MarketFilter(status="open", limit=limit))
            return [m for m in pool if not _is_noise(m)]

        per = int(discovery.get("per_series_limit") or 100)
        out: list[Market] = []
        for st in series:
            try:
                ms = await self.list_markets(MarketFilter(series_key=st, status="open", limit=per))
                out.extend(m for m in ms if not _is_noise(m))
            except Exception as exc:  # noqa: BLE001
                log.warning("kalshi: series '%s' failed: %s", st, exc)
        log.info("kalshi: discovered %d markets across %d series", len(out), len(series))
        return out

    async def get_order_book(self, market_id: str) -> MarketBook:
        data = await self._http.get_json(f"/markets/{market_id}/orderbook")
        return parse_order_book(market_id, data, datetime.now(UTC))

    def fee_model(self, series_key: str | None = None) -> FeeModel:
        return KalshiFeeModel()

    async def aclose(self) -> None:
        await self._http.aclose()
