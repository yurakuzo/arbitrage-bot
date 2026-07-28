"""Live order-book feeds.

Two implementations, both yielding normalized `MarketBook`s onto a shared queue:

- `run_polling_feed`  : REST-polls `get_order_book` on an interval. Works for any
                        venue with no credentials — used for Kalshi (whose WS
                        requires RSA auth, deferred to Phase 4).
- `PolymarketWsFeed`  : real WebSocket to Polymarket's public market channel;
                        maintains books from the `book` snapshot + `price_change`
                        deltas, reconnecting with backoff on failure.

Both funnel errors to the AnomalyReporter and stop cleanly when the stop event is
set.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import websockets

from arb.infra.anomaly import AnomalyReporter, Severity
from arb.infra.logging import get_logger
from arb.providers.base import Provider
from arb.providers.models import MarketBook, Outcome, OutcomeBook, PriceLevel, Venue

log = get_logger(__name__)

POLY_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


async def run_polling_feed(
    provider: Provider,
    market_ids: list[str],
    queue: asyncio.Queue,
    anomaly: AnomalyReporter,
    stop: asyncio.Event,
    interval: float = 3.0,
) -> None:
    while not stop.is_set():
        for mid in market_ids:
            if stop.is_set():
                break
            try:
                book = await provider.get_order_book(mid)
                await queue.put(book)
            except Exception as exc:  # noqa: BLE001 — a fetch error is an anomaly, not fatal
                await anomaly.report(
                    Severity.WARNING, "feed_fetch", f"{provider.venue.value}/{mid}: {exc}"
                )
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except TimeoutError:
            pass


def _ob(bids: dict[float, float], asks: dict[float, float]) -> OutcomeBook:
    return OutcomeBook(
        asks=sorted((PriceLevel(p, s) for p, s in asks.items() if s > 0), key=lambda lv: lv.price),
        bids=sorted(
            (PriceLevel(p, s) for p, s in bids.items() if s > 0),
            key=lambda lv: lv.price,
            reverse=True,
        ),
    )


class PolymarketWsFeed:
    """Maintains YES/NO books per market from the Polymarket market channel."""

    def __init__(
        self,
        provider: Provider,
        market_ids: list[str],  # condition_ids
        queue: asyncio.Queue,
        anomaly: AnomalyReporter,
        stop: asyncio.Event,
    ):
        self.provider = provider
        self.market_ids = market_ids
        self.queue = queue
        self.anomaly = anomaly
        self.stop = stop
        # asset_id -> (condition_id, outcome)
        self.asset_map: dict[str, tuple[str, Outcome]] = {}
        # condition_id -> {outcome -> {"bids": {price:size}, "asks": {price:size}}}
        self.state: dict[str, dict[Outcome, dict[str, dict[float, float]]]] = {}

    async def _resolve(self) -> None:
        for cid in self.market_ids:
            try:
                yes_t, no_t = await self.provider._resolve_tokens(cid)
                self.asset_map[yes_t] = (cid, Outcome.YES)
                self.asset_map[no_t] = (cid, Outcome.NO)
                self.state[cid] = {
                    Outcome.YES: {"bids": {}, "asks": {}},
                    Outcome.NO: {"bids": {}, "asks": {}},
                }
            except Exception as exc:  # noqa: BLE001
                await self.anomaly.report(Severity.WARNING, "token_resolve", f"{cid}: {exc}")

    def _apply_book(self, asset_id: str, bids: list, asks: list) -> str | None:
        entry = self.asset_map.get(asset_id)
        if not entry:
            return None
        cid, outcome = entry
        side = self.state[cid][outcome]
        side["bids"] = {float(b["price"]): float(b["size"]) for b in bids}
        side["asks"] = {float(a["price"]): float(a["size"]) for a in asks}
        return cid

    def _apply_change(self, change: dict) -> str | None:
        entry = self.asset_map.get(str(change.get("asset_id")))
        if not entry:
            return None
        cid, outcome = entry
        price, size = float(change["price"]), float(change["size"])
        ladder = "bids" if str(change.get("side", "")).upper() in ("BUY", "BID") else "asks"
        book_side = self.state[cid][outcome][ladder]
        if size <= 0:
            book_side.pop(price, None)
        else:
            book_side[price] = size
        return cid

    def _emit(self, cid: str) -> MarketBook:
        st = self.state[cid]
        return MarketBook(
            venue=Venue.POLYMARKET,
            market_id=cid,
            ts=datetime.now(UTC),
            yes=_ob(st[Outcome.YES]["bids"], st[Outcome.YES]["asks"]),
            no=_ob(st[Outcome.NO]["bids"], st[Outcome.NO]["asks"]),
        )

    async def _handle(self, raw: str) -> None:
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return  # e.g. "PONG" keepalive echo
        events = data if isinstance(data, list) else [data]
        touched: set[str] = set()
        for ev in events:
            et = ev.get("event_type")
            if et == "book":
                cid = self._apply_book(ev.get("asset_id"), ev.get("bids", []), ev.get("asks", []))
                if cid:
                    touched.add(cid)
            elif et == "price_change":
                for ch in ev.get("price_changes", []):
                    cid = self._apply_change(ch)
                    if cid:
                        touched.add(cid)
        for cid in touched:
            await self.queue.put(self._emit(cid))

    async def _keepalive(self, ws) -> None:
        try:
            while not self.stop.is_set():
                await asyncio.sleep(5)
                await ws.send("PING")
        except (asyncio.CancelledError, websockets.WebSocketException):
            pass

    async def _reemit(self, interval: float = 2.0) -> None:
        """Re-publish current books periodically so a healthy socket keeps them
        fresh. Lives for the connection's lifetime — if the socket drops this
        task is cancelled, letting the books correctly age out to stale."""
        try:
            while not self.stop.is_set():
                await asyncio.sleep(interval)
                for cid in list(self.state):
                    await self.queue.put(self._emit(cid))
        except asyncio.CancelledError:
            pass

    async def run(self) -> None:
        await self._resolve()
        if not self.asset_map:
            return
        backoff = 1.0
        while not self.stop.is_set():
            helpers: list[asyncio.Task] = []
            try:
                async with websockets.connect(POLY_WS_URL, open_timeout=15, ping_interval=None) as ws:
                    await ws.send(json.dumps({"assets_ids": list(self.asset_map), "type": "market"}))
                    helpers = [
                        asyncio.create_task(self._keepalive(ws)),
                        asyncio.create_task(self._reemit()),
                    ]
                    backoff = 1.0
                    while not self.stop.is_set():
                        raw = await asyncio.wait_for(ws.recv(), timeout=30)
                        await self._handle(raw)
            except TimeoutError:
                continue
            except Exception as exc:  # noqa: BLE001 — reconnect on any WS error
                if self.stop.is_set():
                    break
                await self.anomaly.report(Severity.WARNING, "ws_reconnect", f"polymarket: {exc}")
                await asyncio.sleep(backoff)
                backoff = min(30.0, backoff * 2)
            finally:
                for h in helpers:
                    h.cancel()
