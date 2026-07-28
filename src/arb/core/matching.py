"""Cross-venue market matching.

The hardest and most dangerous part of the whole system: deciding that a Kalshi
market and a Polymarket market resolve on the *same* real-world outcome. A false
match manufactures fake arbitrage and causes real losses, so matching is
**curated, not automatic**:

  - `load_mappings()` reads a human-maintained `config/markets.yaml` — the source
    of truth for what is actually the same market (with the outcome sides aligned).
  - `suggest_matches()` only *proposes* candidate pairs (by title similarity) for a
    human to review and promote into markets.yaml. It never auto-confirms.

Outcome alignment matters: two venues may frame the same event with opposite
YES/NO wording, so each mapping records which side maps to which.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from arb.providers.models import Market, Outcome

# Words that carry no matching signal — dropped before comparing titles.
_STOPWORDS = {
    "the", "a", "an", "of", "in", "on", "at", "to", "for", "and", "or", "be",
    "will", "is", "are", "by", "this", "that", "than", "then", "over", "under",
    "before", "after", "market", "resolve", "resolves", "yes", "no",
}
_TOKEN_RE = re.compile(r"[a-z0-9]+")


@dataclass(frozen=True, slots=True)
class OutcomeMapping:
    """One venue's side of a canonical pair."""

    venue: str
    market_id: str
    outcome: Outcome = Outcome.YES


@dataclass(frozen=True, slots=True)
class CanonicalPair:
    """A human-confirmed equivalence between markets across venues."""

    canonical_id: str
    description: str
    legs: dict[str, OutcomeMapping]  # keyed by venue name

    def venues(self) -> list[str]:
        return sorted(self.legs)


@dataclass(frozen=True, slots=True)
class MatchSuggestion:
    score: float
    a: Market
    b: Market
    shared_terms: list[str] = field(default_factory=list)


def _coerce_outcome(value: object) -> Outcome:
    # YAML 1.1 parses bare yes/no as booleans, so `outcome: yes` arrives as True.
    if isinstance(value, bool):
        return Outcome.YES if value else Outcome.NO
    return Outcome(str(value).lower())


def load_mappings(path: Path | str) -> list[CanonicalPair]:
    """Load curated canonical mappings from YAML. Returns [] if the file is absent."""
    p = Path(path)
    if not p.exists():
        return []
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    pairs: list[CanonicalPair] = []
    for entry in data.get("pairs", []):
        legs: dict[str, OutcomeMapping] = {}
        for venue, spec in (entry.get("legs") or {}).items():
            if isinstance(spec, str):
                spec = {"market_id": spec}
            legs[venue] = OutcomeMapping(
                venue=venue,
                market_id=spec["market_id"],
                outcome=_coerce_outcome(spec.get("outcome", "yes")),
            )
        pairs.append(
            CanonicalPair(
                canonical_id=entry["canonical_id"],
                description=entry.get("description", ""),
                legs=legs,
            )
        )
    return pairs


def _tokens(title: str) -> set[str]:
    return {t for t in _TOKEN_RE.findall(title.lower()) if t not in _STOPWORDS and len(t) > 1}


def _similarity(a: str, b: str) -> tuple[float, list[str]]:
    """Jaccard similarity over meaningful title tokens, plus the shared terms."""
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0, []
    shared = ta & tb
    union = ta | tb
    return len(shared) / len(union), sorted(shared)


def suggest_matches(
    markets_a: list[Market],
    markets_b: list[Market],
    *,
    threshold: float = 0.25,
    top_k: int = 50,
) -> list[MatchSuggestion]:
    """Propose candidate cross-venue matches by title similarity.

    These are *suggestions for human review*, not confirmed matches. Ranked by
    score; only pairs at or above `threshold` are returned.
    """
    suggestions: list[MatchSuggestion] = []
    for a in markets_a:
        for b in markets_b:
            score, shared = _similarity(a.title, b.title)
            if score >= threshold:
                suggestions.append(MatchSuggestion(score=score, a=a, b=b, shared_terms=shared))
    suggestions.sort(key=lambda s: s.score, reverse=True)
    return suggestions[:top_k]
