"""Tests for cross-venue matching: curated mapping loader + suggestion heuristic."""

from __future__ import annotations

from arb.core.matching import load_mappings, suggest_matches
from arb.providers.models import Market, Outcome, Venue


def _m(venue, mid, title):
    return Market(venue=venue, market_id=mid, title=title)


def test_load_mappings(tmp_path):
    f = tmp_path / "markets.yaml"
    f.write_text(
        """
pairs:
  - canonical_id: fed-cut-sep
    description: Fed cuts in September
    legs:
      kalshi:
        market_id: KXFED-26SEP-C25
        outcome: yes
      polymarket:
        market_id: "0xabc"
        outcome: no
  - canonical_id: shorthand
    description: bare-id form
    legs:
      kalshi: KXFOO
      polymarket: "0xdef"
""",
        encoding="utf-8",
    )
    pairs = load_mappings(f)
    assert len(pairs) == 2
    fed = pairs[0]
    assert fed.canonical_id == "fed-cut-sep"
    assert fed.legs["polymarket"].outcome is Outcome.NO
    assert fed.legs["kalshi"].outcome is Outcome.YES
    # Shorthand string form defaults to YES.
    assert pairs[1].legs["kalshi"].market_id == "KXFOO"
    assert pairs[1].legs["kalshi"].outcome is Outcome.YES


def test_load_mappings_missing_file(tmp_path):
    assert load_mappings(tmp_path / "nope.yaml") == []


def test_suggest_matches_ranks_by_shared_terms():
    kalshi = [
        _m(Venue.KALSHI, "K1", "Will the high temp in NYC exceed 90 degrees"),
        _m(Venue.KALSHI, "K2", "Will the Yankees win the World Series"),
    ]
    poly = [
        _m(Venue.POLYMARKET, "P1", "Will NYC high temp exceed 90 degrees today"),
        _m(Venue.POLYMARKET, "P2", "Rihanna album before GTA VI"),
    ]
    sugg = suggest_matches(kalshi, poly, threshold=0.1)
    assert sugg, "expected at least one suggestion"
    top = sugg[0]
    assert top.a.market_id == "K1" and top.b.market_id == "P1"
    assert {"nyc", "temp", "90", "degrees"} & set(top.shared_terms)


def test_suggest_matches_threshold_filters():
    kalshi = [_m(Venue.KALSHI, "K", "Fed interest rate decision September")]
    poly = [_m(Venue.POLYMARKET, "P", "Rihanna new album release")]
    assert suggest_matches(kalshi, poly, threshold=0.5) == []
