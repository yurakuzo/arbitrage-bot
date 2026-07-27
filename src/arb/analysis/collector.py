"""Analysis-mode collector: sweep markets across venues and snapshot to SQLite.

This is the cron entrypoint. Each run:
  1. lists the top markets per venue (by the venue's own volume ordering),
  2. fetches an order-book snapshot for each (bounded concurrency),
  3. upserts market metadata and appends a book snapshot row.

Errors on a single market are logged and skipped so one bad market never aborts
the whole run.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

from arb.infra.db import Database
from arb.infra.logging import get_logger
from arb.providers.base import Provider
from arb.providers.models import Market, MarketBook, Outcome

log = get_logger(__name__)

_CONCURRENCY = 8
# List this many times `limit` markets, then snapshot the top `limit` by
# liquidity/volume. Lets us pick the *viable* markets even when a venue doesn't
# return them in popularity order (Kalshi lists provisional markets first).
_POOL_FACTOR = 10
_POOL_CAP = 1000


def _rank_key(m: Market) -> tuple[float, float]:
    return (m.raw.get("liquidity") or 0.0, m.raw.get("volume") or 0.0)


def _depth(book: MarketBook, outcome: Outcome) -> float:
    return sum(lv.size for lv in book.book_for(outcome).asks)


def _upsert_market(conn, m: Market, ts_iso: str) -> None:
    conn.execute(
        """
        INSERT INTO markets (venue, market_id, title, series_key, close_time, first_seen, last_seen)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(venue, market_id) DO UPDATE SET
            title=excluded.title,
            series_key=excluded.series_key,
            close_time=excluded.close_time,
            last_seen=excluded.last_seen
        """,
        (
            m.venue.value,
            m.market_id,
            m.title,
            m.series_key,
            m.close_time.isoformat() if m.close_time else None,
            ts_iso,
            ts_iso,
        ),
    )


def _insert_snapshot(conn, m: Market, book: MarketBook, ts_iso: str) -> None:
    conn.execute(
        """
        INSERT INTO book_snapshots
            (ts, venue, market_id, yes_best_ask, yes_best_bid, no_best_ask, no_best_bid,
             yes_ask_depth, no_ask_depth, volume, liquidity, raw)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            ts_iso,
            m.venue.value,
            m.market_id,
            book.yes.best_ask,
            book.yes.best_bid,
            book.no.best_ask,
            book.no.best_bid,
            _depth(book, Outcome.YES),
            _depth(book, Outcome.NO),
            m.raw.get("volume"),
            m.raw.get("liquidity"),
            None,  # full ladder JSON omitted to keep the DB small; add if needed
        ),
    )
    _ = json  # reserved for optional raw-ladder persistence


async def _fetch_book(provider: Provider, market: Market, sem: asyncio.Semaphore):
    async with sem:
        try:
            return market, await provider.get_order_book(market.market_id)
        except Exception as exc:  # noqa: BLE001 — skip one bad market, keep the run
            log.warning("book fetch failed for %s/%s: %s", market.venue.value, market.market_id, exc)
            return market, None


async def collect_venue(
    provider: Provider, db: Database, limit: int, discovery: dict | None = None
) -> int:
    ts = datetime.now(UTC)
    ts_iso = ts.isoformat()
    pool_size = min(_POOL_CAP, max(limit, limit * _POOL_FACTOR))
    pool = await provider.discover(pool_size, discovery)
    markets = sorted(pool, key=_rank_key, reverse=True)[:limit]
    log.info(
        "%s: discovered %d markets, snapshotting top %d by liquidity/volume",
        provider.venue.value, len(pool), len(markets),
    )

    sem = asyncio.Semaphore(_CONCURRENCY)
    results = await asyncio.gather(*(_fetch_book(provider, m, sem) for m in markets))

    stored = 0
    with db.transaction() as conn:
        for market, book in results:
            _upsert_market(conn, market, ts_iso)
            if book is None:
                continue
            _insert_snapshot(conn, market, book, ts_iso)
            stored += 1
    log.info("%s: stored %d book snapshots", provider.venue.value, stored)
    return stored


async def run_collection(
    providers: dict[str, Provider],
    db: Database,
    limit: int,
    discovery: dict | None = None,
) -> dict[str, int]:
    db.init_schema()
    discovery = discovery or {}
    counts: dict[str, int] = {}
    try:
        for name, provider in providers.items():
            try:
                counts[name] = await collect_venue(provider, db, limit, discovery.get(name))
            except Exception as exc:  # noqa: BLE001 — isolate venue failures
                log.error("collection failed for venue %s: %s", name, exc)
                counts[name] = 0
    finally:
        for provider in providers.values():
            close = getattr(provider, "aclose", None)
            if close:
                await close()
    return counts
