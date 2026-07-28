"""P&L summary over the paper-trade ledger (`paper_fills`).

Each recorded arbitrage writes one row per leg sharing the same (ts, canonical_id).
A matched pair pays exactly $1 per contract at resolution regardless of outcome,
so for one trade:

    net = contracts - sum(price*contracts over legs) - sum(fee over legs)

We recompute from the fills themselves (rather than trusting a stored number) so
the summary is self-contained and verifiable.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from arb.infra.db import Database


@dataclass(frozen=True, slots=True)
class PnlSummary:
    trades: int
    contracts: float
    gross_cost: float
    fees: float
    net_profit: float
    roi: float
    by_pair: pd.DataFrame  # canonical_id, trades, net_profit
    by_venue: pd.DataFrame  # venue, legs, contracts, fees, cost
    recent: pd.DataFrame  # most-recent trades

    @property
    def is_empty(self) -> bool:
        return self.trades == 0


def _load_fills(db: Database) -> pd.DataFrame:
    with db.transaction() as conn:
        return pd.read_sql_query(
            "SELECT ts, canonical_id, venue, market_id, outcome, price, contracts, fee "
            "FROM paper_fills",
            conn,
        )


def _trades_from_fills(fills: pd.DataFrame) -> pd.DataFrame:
    fills = fills.copy()
    fills["cost"] = fills["price"] * fills["contracts"]
    grp = fills.groupby(["ts", "canonical_id"], as_index=False).agg(
        contracts=("contracts", "max"),  # legs share the same size
        cost=("cost", "sum"),
        fees=("fee", "sum"),
    )
    grp["net_profit"] = grp["contracts"] - grp["cost"] - grp["fees"]
    return grp


def summarize(db: Database, recent_limit: int = 10) -> PnlSummary:
    fills = _load_fills(db)
    if fills.empty:
        empty = pd.DataFrame()
        return PnlSummary(0, 0.0, 0.0, 0.0, 0.0, 0.0, empty, empty, empty)

    trades = _trades_from_fills(fills)
    net = float(trades["net_profit"].sum())
    cost = float(trades["cost"].sum())
    fees = float(trades["fees"].sum())
    contracts = float(trades["contracts"].sum())

    by_pair = (
        trades.groupby("canonical_id", as_index=False)
        .agg(trades=("net_profit", "count"), net_profit=("net_profit", "sum"))
        .sort_values("net_profit", ascending=False)
    )
    by_venue = (
        fills.assign(cost=fills["price"] * fills["contracts"])
        .groupby("venue", as_index=False)
        .agg(legs=("venue", "count"), contracts=("contracts", "sum"),
             fees=("fee", "sum"), cost=("cost", "sum"))
    )
    recent = trades.sort_values("ts", ascending=False).head(recent_limit)

    return PnlSummary(
        trades=len(trades),
        contracts=contracts,
        gross_cost=cost,
        fees=fees,
        net_profit=net,
        roi=net / cost if cost else 0.0,
        by_pair=by_pair,
        by_venue=by_venue,
        recent=recent,
    )
