"""Smoke tests: package imports, DB schema builds, models behave."""

from __future__ import annotations

from datetime import datetime, timezone

from arb.config import AppConfig, load_app_config
from arb.infra.db import Database
from arb.providers.models import (
    MarketBook,
    Outcome,
    OutcomeBook,
    PriceLevel,
    Venue,
)


def test_config_loads_example_defaults():
    cfg = load_app_config()
    assert isinstance(cfg, AppConfig)
    assert "kalshi" in cfg.venues
    assert cfg.thresholds.max_quote_age_ms > 0


def test_db_schema_initializes(tmp_path):
    db = Database(tmp_path / "t.sqlite")
    db.init_schema()
    with db.transaction() as conn:
        tables = {
            r["name"]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert {"markets", "book_snapshots", "spreads", "paper_fills"} <= tables


def test_market_book_best_prices_and_lookup():
    book = MarketBook(
        venue=Venue.KALSHI,
        market_id="KXTEST-1",
        ts=datetime.now(timezone.utc),
        yes=OutcomeBook(asks=[PriceLevel(0.40, 100), PriceLevel(0.41, 50)]),
        no=OutcomeBook(asks=[PriceLevel(0.58, 200)]),
    )
    assert book.yes.best_ask == 0.40
    assert book.book_for(Outcome.NO).best_ask == 0.58
