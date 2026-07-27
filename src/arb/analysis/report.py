"""Analysis-mode report: turn collected snapshots into an Excel workbook.

Produces two sheets:
  - "markets": one row per market (latest snapshot) with prices, spread, depth,
    volume/liquidity, and cross-run stats (sample count + price volatility).
  - "summary": per-venue and per-series counts to eyeball coverage.

The intent is a scannable table to shortlist which repeatable markets are worth
tracking live (Phase 2 does cross-venue matching on top of this).
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from arb.infra.db import Database
from arb.infra.logging import get_logger

log = get_logger(__name__)

_LATEST_SQL = """
SELECT b.*
FROM book_snapshots b
JOIN (
    SELECT venue, market_id, MAX(ts) AS mts
    FROM book_snapshots GROUP BY venue, market_id
) x ON b.venue = x.venue AND b.market_id = x.market_id AND b.ts = x.mts
"""

_HISTORY_SQL = """
SELECT venue, market_id, yes_best_ask, yes_best_bid
FROM book_snapshots
"""


def _load(db: Database) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    with db.transaction() as conn:
        latest = pd.read_sql_query(_LATEST_SQL, conn)
        history = pd.read_sql_query(_HISTORY_SQL, conn)
        markets = pd.read_sql_query("SELECT * FROM markets", conn)
    return latest, history, markets


def build_dataframe(db: Database) -> pd.DataFrame:
    latest, history, markets = _load(db)
    if latest.empty:
        return latest

    # Cross-run stats: sample count and volatility of the YES mid price.
    history["yes_mid"] = (history["yes_best_ask"] + history["yes_best_bid"]) / 2
    stats = (
        history.groupby(["venue", "market_id"])["yes_mid"]
        .agg(samples="count", yes_mid_volatility="std")
        .reset_index()
    )

    df = latest.merge(
        markets[["venue", "market_id", "title", "series_key", "close_time"]],
        on=["venue", "market_id"],
        how="left",
    ).merge(stats, on=["venue", "market_id"], how="left")

    # Derived analytics.
    df["yes_spread"] = df["yes_best_ask"] - df["yes_best_bid"]
    df["no_spread"] = df["no_best_ask"] - df["no_best_bid"]
    # Internal book sum: buy-YES + buy-NO on the SAME venue. < 1.0 would itself be
    # an intra-venue arbitrage (usually a data quirk); a useful sanity flag.
    df["internal_yes_plus_no_ask"] = df["yes_best_ask"] + df["no_best_ask"]

    cols = [
        "venue", "title", "market_id", "series_key", "close_time",
        "yes_best_bid", "yes_best_ask", "yes_spread",
        "no_best_bid", "no_best_ask", "no_spread",
        "internal_yes_plus_no_ask",
        "yes_ask_depth", "no_ask_depth",
        "volume", "liquidity",
        "samples", "yes_mid_volatility",
        "ts",
    ]
    df = df[[c for c in cols if c in df.columns]]
    return df.sort_values(["liquidity", "volume"], ascending=False, na_position="last")


def _summary(df: pd.DataFrame) -> pd.DataFrame:
    per_venue = (
        df.groupby("venue")
        .agg(
            markets=("market_id", "count"),
            avg_liquidity=("liquidity", "mean"),
            total_volume=("volume", "sum"),
        )
        .reset_index()
    )
    return per_venue


def _autoformat(writer: pd.ExcelWriter, sheet: str, df: pd.DataFrame) -> None:
    ws = writer.sheets[sheet]
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    for i, col in enumerate(df.columns, start=1):
        maxlen = df[col].astype(str).str.len().max()
        maxlen = 12 if pd.isna(maxlen) else int(maxlen)  # all-NaN columns -> default
        width = min(45, max(12, maxlen + 2, len(str(col)) + 2))
        ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = width


def write_report(db: Database, out_path: Path | str) -> Path:
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    df = build_dataframe(db)
    if df.empty:
        log.warning("No snapshots found — run `arb collect` first.")
    with pd.ExcelWriter(out, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="markets", index=False)
        if not df.empty:
            _summary(df).to_excel(writer, sheet_name="summary", index=False)
            _autoformat(writer, "markets", df)
    log.info("Wrote %d market rows to %s", len(df), out)
    return out
