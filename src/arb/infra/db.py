"""SQLite time-series store.

Holds the raw material for analysis mode (order-book snapshots), the derived
cross-venue spread observations, and the paper-trading fill ledger. Kept simple
and dependency-free (stdlib sqlite3); schema is created idempotently.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
-- Market discovery snapshots (what markets existed at collection time).
CREATE TABLE IF NOT EXISTS markets (
    id          INTEGER PRIMARY KEY,
    venue       TEXT NOT NULL,
    market_id   TEXT NOT NULL,
    title       TEXT,
    subtitle    TEXT,          -- specific outcome (Kalshi yes_sub_title / Poly candidate)
    series_key  TEXT,
    close_time  TEXT,
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    UNIQUE(venue, market_id)
);

-- Order-book snapshots (top-of-book + summary stats per collection run).
CREATE TABLE IF NOT EXISTS book_snapshots (
    id            INTEGER PRIMARY KEY,
    ts            TEXT NOT NULL,
    venue         TEXT NOT NULL,
    market_id     TEXT NOT NULL,
    yes_best_ask  REAL,
    yes_best_bid  REAL,
    no_best_ask   REAL,
    no_best_bid   REAL,
    yes_ask_depth REAL,   -- contracts available on the yes-ask ladder
    no_ask_depth  REAL,
    volume        REAL,    -- market lifetime volume at snapshot time
    liquidity     REAL,    -- market resting liquidity at snapshot time
    raw           TEXT     -- JSON blob of the full normalized book (optional)
);
CREATE INDEX IF NOT EXISTS ix_book_market_ts
    ON book_snapshots(venue, market_id, ts);

-- Cross-venue spread observations (analysis output; one row per candidate pair/run).
CREATE TABLE IF NOT EXISTS spreads (
    id            INTEGER PRIMARY KEY,
    ts            TEXT NOT NULL,
    canonical_id  TEXT NOT NULL,
    venue_a       TEXT NOT NULL,
    market_a      TEXT NOT NULL,
    venue_b       TEXT NOT NULL,
    market_b      TEXT NOT NULL,
    gross_edge    REAL,   -- 1 - (buy_yes_a + buy_no_b), before fees
    net_edge      REAL,   -- after fees/slippage estimate
    tradable_size REAL    -- contracts executable at that edge
);
CREATE INDEX IF NOT EXISTS ix_spreads_canonical_ts
    ON spreads(canonical_id, ts);

-- Paper-trading fill ledger (simulator output; no real orders).
CREATE TABLE IF NOT EXISTS paper_fills (
    id            INTEGER PRIMARY KEY,
    ts            TEXT NOT NULL,
    canonical_id  TEXT NOT NULL,
    venue         TEXT NOT NULL,
    market_id     TEXT NOT NULL,
    outcome       TEXT NOT NULL,
    price         REAL NOT NULL,
    contracts     REAL NOT NULL,
    fee           REAL NOT NULL,
    note          TEXT
);
"""


class Database:
    def __init__(self, path: Path | str):
        self.path = Path(path)

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        return conn

    def init_schema(self) -> None:
        with self.transaction() as conn:
            conn.executescript(SCHEMA)
            # Lightweight migrations for pre-existing DBs (ADD COLUMN is a no-op
            # on fresh schemas; ignore "duplicate column" on already-migrated ones).
            for stmt in ("ALTER TABLE markets ADD COLUMN subtitle TEXT",):
                try:
                    conn.execute(stmt)
                except sqlite3.OperationalError:
                    pass

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
