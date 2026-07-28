"""Phase 2 shortlist: rank curated cross-venue pairs by fee-net edge.

Reads the human-confirmed mappings in `config/markets.yaml`, pulls the latest
snapshot for each leg from SQLite, and computes the fee-net arbitrage edge in
both directions. Produces a ranked table (and an Excel sheet) to decide which
pairs are worth tracking live in Phase 3.

Edge is evaluated at *top of book* using stored best-ask + ask-depth, so the
size/profit figures are an optimistic proxy — exact, book-walked sizing happens
live in Phase 3 (`core.pricing.find_arbitrage`). Ranking on the per-contract net
edge is the reliable signal here.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from arb.core.fees import FeeModel, KalshiFeeModel, PolymarketFeeModel
from arb.core.matching import CanonicalPair, load_mappings
from arb.core.pricing import top_of_book_edge
from arb.infra.db import Database
from arb.infra.logging import get_logger
from arb.providers.models import Outcome

log = get_logger(__name__)

_FEE_MODELS: dict[str, FeeModel] = {
    "kalshi": KalshiFeeModel(),
    "polymarket": PolymarketFeeModel(),
}


def _opposite(o: Outcome) -> Outcome:
    return Outcome.NO if o is Outcome.YES else Outcome.YES


@dataclass(frozen=True, slots=True)
class LegQuote:
    venue: str
    market_id: str
    yes_ask: float | None
    no_ask: float | None
    yes_depth: float | None
    no_depth: float | None

    def ask(self, o: Outcome) -> float | None:
        return self.yes_ask if o is Outcome.YES else self.no_ask

    def depth(self, o: Outcome) -> float | None:
        return self.yes_depth if o is Outcome.YES else self.no_depth


def _latest_quote(conn, venue: str, market_id: str) -> LegQuote | None:
    row = conn.execute(
        """
        SELECT yes_best_ask, no_best_ask, yes_ask_depth, no_ask_depth
        FROM book_snapshots
        WHERE venue = ? AND market_id = ?
        ORDER BY ts DESC LIMIT 1
        """,
        (venue, market_id),
    ).fetchone()
    if row is None:
        return None
    return LegQuote(venue, market_id, row[0], row[1], row[2], row[3])


def _evaluate_pair(pair: CanonicalPair, quotes: dict[str, LegQuote]) -> dict | None:
    """Best fee-net direction for one canonical pair, or None if unpriced.

    The mapping aligns each venue's "event happens" outcome. Arbitrage buys
    "happens" on one venue and "doesn't happen" (opposite) on the other.
    """
    venues = pair.venues()
    if len(venues) != 2:
        return None
    va, vb = venues
    qa, qb = quotes.get(va), quotes.get(vb)
    if qa is None or qb is None:
        return None
    oa, ob = pair.legs[va].outcome, pair.legs[vb].outcome
    fa, fb = _FEE_MODELS.get(va, PolymarketFeeModel()), _FEE_MODELS.get(vb, PolymarketFeeModel())

    # Direction 1: buy `oa` on A + opposite(`ob`) on B.
    # Direction 2: buy opposite(`oa`) on A + `ob` on B.
    directions = [
        (oa, _opposite(ob)),
        (_opposite(oa), ob),
    ]
    best = None
    for da, db in directions:
        edge = top_of_book_edge(qa.ask(da), qb.ask(db), fa, fb, contracts=1.0)
        if edge is None:
            continue
        depth = min(x for x in (qa.depth(da), qb.depth(db)) if x is not None) if (
            qa.depth(da) is not None and qb.depth(db) is not None
        ) else 0.0
        cand = {
            "net_edge_per_contract": edge,
            "buy_a": f"{va}:{da.value}",
            "buy_b": f"{vb}:{db.value}",
            "price_a": qa.ask(da),
            "price_b": qb.ask(db),
            "tradable_contracts": depth,
            "est_net_profit": edge * depth,  # optimistic (top-of-book) proxy
        }
        if best is None or cand["net_edge_per_contract"] > best["net_edge_per_contract"]:
            best = cand
    if best is None:
        return None
    return {
        "canonical_id": pair.canonical_id,
        "description": pair.description,
        **best,
    }


def build_shortlist(db: Database, mappings_path: Path | str) -> pd.DataFrame:
    pairs = load_mappings(mappings_path)
    if not pairs:
        log.warning("No canonical mappings in %s — nothing to shortlist.", mappings_path)
        return pd.DataFrame()

    rows: list[dict] = []
    with db.transaction() as conn:
        for pair in pairs:
            quotes = {
                v: _latest_quote(conn, v, leg.market_id) for v, leg in pair.legs.items()
            }
            result = _evaluate_pair(pair, {k: q for k, q in quotes.items() if q})
            if result is None:
                log.warning("pair '%s': missing snapshot(s), skipped", pair.canonical_id)
                continue
            rows.append(result)

    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values(
            ["net_edge_per_contract", "tradable_contracts"], ascending=False
        ).reset_index(drop=True)
    return df


def write_shortlist(db: Database, mappings_path: Path | str, out_path: Path | str) -> pd.DataFrame:
    df = build_shortlist(db, mappings_path)
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(out, engine="openpyxl") as writer:
        (df if not df.empty else pd.DataFrame({"info": ["no priced mappings"]})).to_excel(
            writer, sheet_name="shortlist", index=False
        )
    log.info("Wrote shortlist (%d pairs) to %s", len(df), out)
    return df
