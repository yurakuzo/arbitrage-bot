"""Live paper-trading engine.

Consumes a stream of order-book updates from both venues, and for each curated
canonical pair computes the fee-net arbitrage in real time. Profitable, fresh
opportunities are sized against a stake cap and recorded by the paper simulator;
staleness and errors are routed to the anomaly reporter (→ Telegram).

The pure evaluation logic (`evaluate`) is separated from the async plumbing so it
can be unit-tested with synthetic books and no network.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from arb.config import ThresholdConfig
from arb.core.fees import FeeModel, KalshiFeeModel, PolymarketFeeModel
from arb.core.matching import CanonicalPair
from arb.core.pricing import ArbLeg, ArbOpportunity, find_arbitrage
from arb.infra.anomaly import AnomalyReporter, Severity
from arb.infra.logging import get_logger
from arb.live.simulator import PaperLedger
from arb.providers.base import Provider
from arb.providers.models import MarketBook, Outcome

log = get_logger(__name__)

_FEE_MODELS: dict[str, FeeModel] = {
    "kalshi": KalshiFeeModel(),
    "polymarket": PolymarketFeeModel(),
}


def _swap(book: MarketBook) -> MarketBook:
    """Return a copy with YES/NO books swapped (opposite outcome framing)."""
    return replace(book, yes=book.no, no=book.yes)


def canonicalize(book: MarketBook, event_outcome: Outcome) -> MarketBook:
    """Orient a book so YES == 'the event happens', per the mapping's alignment."""
    return book if event_outcome is Outcome.YES else _swap(book)


def _scale(opp: ArbOpportunity, factor: float) -> ArbOpportunity:
    """Scale an opportunity down to a stake cap (paper approximation)."""
    if factor >= 1.0:
        return opp
    legs = tuple(
        ArbLeg(lg.venue, lg.outcome, lg.contracts * factor, lg.avg_price,
               lg.cost * factor, lg.fee * factor)
        for lg in opp.legs
    )
    return ArbOpportunity(
        contracts=opp.contracts * factor,
        gross_profit=opp.gross_profit * factor,
        net_profit=opp.net_profit * factor,
        roi=opp.roi,
        net_edge_per_contract=opp.net_edge_per_contract,
        legs=legs,
    )


@dataclass(frozen=True, slots=True)
class EvalResult:
    status: str  # "arb" | "no_arb" | "stale" | "unpriced"
    opp: ArbOpportunity | None = None
    detail: str = ""


def evaluate(
    pair: CanonicalPair,
    book_a: MarketBook | None,
    book_b: MarketBook | None,
    thresholds: ThresholdConfig,
    now: datetime,
) -> EvalResult:
    """Pure per-pair evaluation: staleness + fee-net arbitrage + stake sizing."""
    if book_a is None or book_b is None:
        return EvalResult("unpriced", detail="missing a leg")

    max_age = thresholds.max_quote_age_ms / 1000.0
    for bk in (book_a, book_b):
        age = (now - bk.ts).total_seconds()
        if age > max_age:
            return EvalResult("stale", detail=f"{bk.venue.value}/{bk.market_id} quote {age:.1f}s old")

    va, vb = book_a.venue.value, book_b.venue.value
    fa, fb = _FEE_MODELS.get(va, PolymarketFeeModel()), _FEE_MODELS.get(vb, PolymarketFeeModel())
    ca = canonicalize(book_a, pair.legs[va].outcome)
    cb = canonicalize(book_b, pair.legs[vb].outcome)

    opp = find_arbitrage(ca, cb, fa, fb, min_net_profit=0.0)
    if opp is None or opp.net_edge_per_contract < thresholds.min_edge:
        return EvalResult("no_arb")

    total_cost = sum(lg.cost for lg in opp.legs)
    if total_cost > thresholds.max_stake_usd:
        opp = _scale(opp, thresholds.max_stake_usd / total_cost)
    return EvalResult("arb", opp=opp)


class Engine:
    def __init__(
        self,
        providers: dict[str, Provider],
        pairs: list[CanonicalPair],
        ledger: PaperLedger,
        anomaly: AnomalyReporter,
        thresholds: ThresholdConfig,
    ):
        self.providers = providers
        self.pairs = pairs
        self.ledger = ledger
        self.anomaly = anomaly
        self.thresholds = thresholds
        self.books: dict[tuple[str, str], MarketBook] = {}
        self._open: set[str] = set()
        # Index: (venue, market_id) -> pairs that involve it.
        self._index: dict[tuple[str, str], list[CanonicalPair]] = {}
        for p in pairs:
            for venue, leg in p.legs.items():
                self._index.setdefault((venue, leg.market_id), []).append(p)

    async def _on_book(self, book: MarketBook) -> None:
        self.books[(book.venue.value, book.market_id)] = book
        now = datetime.now(UTC)
        for pair in self._index.get((book.venue.value, book.market_id), []):
            venues = pair.venues()
            if len(venues) != 2:
                continue
            va, vb = venues
            res = evaluate(
                pair,
                self.books.get((va, pair.legs[va].market_id)),
                self.books.get((vb, pair.legs[vb].market_id)),
                self.thresholds,
                now,
            )
            await self._act(pair, res)

    async def _act(self, pair: CanonicalPair, res: EvalResult) -> None:
        if res.status == "stale":
            await self.anomaly.report(Severity.WARNING, "stale_quote", f"{pair.canonical_id}: {res.detail}")
            return
        if res.status == "arb" and res.opp is not None:
            if pair.canonical_id not in self._open:
                self._open.add(pair.canonical_id)
                self.ledger.record(pair, res.opp, datetime.now(UTC))
                await self.anomaly.report(
                    Severity.INFO,
                    "arb_detected",
                    f"{pair.canonical_id}: net=${res.opp.net_profit:.2f} "
                    f"edge/contract=${res.opp.net_edge_per_contract:.4f}",
                )
        elif pair.canonical_id in self._open:
            # Opportunity closed — reset hysteresis so the next one re-triggers.
            self._open.discard(pair.canonical_id)

    async def run(self, duration_s: float | None = None, force_poll: bool = False) -> None:
        from arb.live.feeds import PolymarketWsFeed, run_polling_feed

        queue: asyncio.Queue[MarketBook] = asyncio.Queue()
        stop = asyncio.Event()
        tasks: list[asyncio.Task] = []

        # Group tracked market ids per venue.
        per_venue: dict[str, list[str]] = {}
        for p in self.pairs:
            for venue, leg in p.legs.items():
                per_venue.setdefault(venue, []).append(leg.market_id)

        for venue, market_ids in per_venue.items():
            provider = self.providers.get(venue)
            if provider is None:
                continue
            market_ids = sorted(set(market_ids))
            use_ws = (venue == "polymarket") and not force_poll
            if use_ws:
                feed = PolymarketWsFeed(provider, market_ids, queue, self.anomaly, stop)
                tasks.append(asyncio.create_task(feed.run(), name=f"ws-{venue}"))
            else:
                tasks.append(
                    asyncio.create_task(
                        run_polling_feed(provider, market_ids, queue, self.anomaly, stop, interval=3.0),
                        name=f"poll-{venue}",
                    )
                )

        log.info("engine: tracking %d pairs across %d venues", len(self.pairs), len(per_venue))
        deadline = None if duration_s is None else asyncio.get_event_loop().time() + duration_s
        try:
            while not stop.is_set():
                timeout = None if deadline is None else max(0.0, deadline - asyncio.get_event_loop().time())
                if deadline is not None and timeout == 0.0:
                    break
                try:
                    book = await asyncio.wait_for(queue.get(), timeout=timeout)
                except TimeoutError:
                    break
                await self._on_book(book)
        finally:
            stop.set()
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for provider in self.providers.values():
                close = getattr(provider, "aclose", None)
                if close:
                    await close()
        log.info(
            "engine stopped: %d paper trades, running P&L=$%.2f",
            self.ledger.trades, self.ledger.realized_profit,
        )
