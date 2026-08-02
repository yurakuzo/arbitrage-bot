"""Tests for cross-venue matching: curated mapping loader + suggestion heuristic."""

from __future__ import annotations

from arb.core.matching import (
    _name_similarity,
    _name_tokens,
    load_mappings,
    render_pairs_yaml,
    suggest_event_pairs,
    suggest_matches,
)
from arb.providers.models import Market, Outcome, Venue


def _m(venue, mid, title, subtitle=None, series_key=None):
    return Market(venue=venue, market_id=mid, title=title, series_key=series_key,
                  raw={"subtitle": subtitle})


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


# --- Outcome-aware matching ------------------------------------------------------
def test_name_tokens_normalizes_initials_and_periods():
    assert _name_tokens("J.D. Vance") == {"jd", "vance"}
    assert _name_tokens("Donald J. Trump") == {"donald", "trump"}  # middle initial dropped


def test_name_similarity():
    assert _name_similarity("J.D. Vance", "JD Vance") == 1.0
    assert _name_similarity("Donald J. Trump", "Donald Trump") == 1.0
    assert _name_similarity("Gavin Newsom", "Kamala Harris") == 0.0


def test_suggest_event_pairs_groups_by_event():
    kalshi = [
        _m(Venue.KALSHI, "KXPRES-GNEWS", "Who will win?", "Gavin Newsom", "KXPRESPERSON"),
        _m(Venue.KALSHI, "KXPRES-JVAN", "Who will win?", "J.D. Vance", "KXPRESPERSON"),
        _m(Venue.KALSHI, "KXPRES-AOC", "Who will win?", "Alexandria Ocasio-Cortez", "KXPRESPERSON"),
    ]
    poly = [
        _m(Venue.POLYMARKET, "0xa", "Will Newsom win?", "Gavin Newsom", "prez-2028"),
        _m(Venue.POLYMARKET, "0xb", "Will Vance win?", "JD Vance", "prez-2028"),
        _m(Venue.POLYMARKET, "0xc", "Will AOC win?", "Alexandria Ocasio-Cortez", "prez-2028"),
    ]
    pairs = suggest_event_pairs(kalshi, poly, name_threshold=0.6, min_shared=3)
    assert len(pairs) == 1
    evp = pairs[0]
    assert evp.series_a == "KXPRESPERSON" and evp.series_b == "prez-2028"
    assert evp.shared == 3

    yaml_text = render_pairs_yaml(evp)
    assert "KXPRES-GNEWS" in yaml_text and "0xa" in yaml_text
    assert "canonical_id: prez-2028-gavin-newsom" in yaml_text


def test_suggest_event_pairs_respects_min_shared():
    kalshi = [_m(Venue.KALSHI, "K1", "t", "Gavin Newsom", "KXA")]
    poly = [_m(Venue.POLYMARKET, "P1", "t", "Gavin Newsom", "pa")]
    assert suggest_event_pairs(kalshi, poly, min_shared=3) == []


def test_suggest_event_pairs_ignores_markets_without_outcome():
    kalshi = [_m(Venue.KALSHI, "K1", "Will US invade Iran?", None, "KXIRAN")]
    poly = [_m(Venue.POLYMARKET, "P1", "Will US invade Iran?", None, "iran")]
    assert suggest_event_pairs(kalshi, poly, min_shared=1) == []
