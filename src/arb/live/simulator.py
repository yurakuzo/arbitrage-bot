"""Paper-trading simulator: records simulated fills and tracks P&L.

v1 places NO real orders. When the engine detects a profitable, fresh arbitrage it
calls the simulator, which assumes both legs fill at the walked average prices,
writes a row per leg into `paper_fills`, and accumulates realized profit. This is
the ledger we use to judge whether the strategy would have made money before any
real capital is risked.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from arb.core.matching import CanonicalPair
from arb.core.pricing import ArbOpportunity
from arb.infra.db import Database
from arb.infra.logging import get_logger

log = get_logger(__name__)


@dataclass
class PaperLedger:
    db: Database
    realized_profit: float = 0.0
    trades: int = 0
    _seen: set[str] = field(default_factory=set)

    def record(self, pair: CanonicalPair, opp: ArbOpportunity, ts: datetime) -> None:
        """Persist a simulated arbitrage (one paper_fills row per leg)."""
        ts_iso = ts.isoformat()
        rows = []
        for leg in opp.legs:
            market_id = pair.legs[leg.venue.value].market_id if leg.venue.value in pair.legs else "?"
            rows.append(
                (
                    ts_iso,
                    pair.canonical_id,
                    leg.venue.value,
                    market_id,
                    leg.outcome.value,
                    leg.avg_price,
                    leg.contracts,
                    leg.fee,
                    f"net={opp.net_profit:.4f} roi={opp.roi:.4f}",
                )
            )
        with self.db.transaction() as conn:
            conn.executemany(
                """
                INSERT INTO paper_fills
                    (ts, canonical_id, venue, market_id, outcome, price, contracts, fee, note)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
        self.realized_profit += opp.net_profit
        self.trades += 1
        log.info(
            "PAPER FILL %s: %.1f contracts, net=$%.2f (running P&L=$%.2f)",
            pair.canonical_id, opp.contracts, opp.net_profit, self.realized_profit,
        )
