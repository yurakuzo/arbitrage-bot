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


# --- Outcome-aware matching (multi-outcome events, e.g. election candidates) ----

def _subtitle(m: Market) -> str | None:
    return m.raw.get("subtitle")


def _name_tokens(name: str) -> set[str]:
    """Normalize a contestant name to comparable tokens.

    Strips periods so 'J.D.' == 'JD', splits on non-alphanumerics, lowercases,
    and drops 1-char tokens (middle initials) so 'Donald J. Trump' == 'Donald
    Trump'.
    """
    cleaned = name.replace(".", "")
    return {t for t in _TOKEN_RE.findall(cleaned.lower()) if len(t) > 1}


def _name_similarity(a: str, b: str) -> float:
    ta, tb = _name_tokens(a), _name_tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


@dataclass(frozen=True, slots=True)
class OutcomeMatch:
    score: float
    a: Market  # e.g. kalshi market
    b: Market  # e.g. polymarket market

    @property
    def name(self) -> str:
        return _subtitle(self.a) or _subtitle(self.b) or ""


@dataclass(frozen=True, slots=True)
class EventPairSuggestion:
    """Two events (one per venue) that share matched outcomes — the strong signal
    that they're the same market family worth pairing."""

    series_a: str | None
    series_b: str | None
    title_a: str
    title_b: str
    matches: list[OutcomeMatch]

    @property
    def shared(self) -> int:
        return len(self.matches)


def suggest_event_pairs(
    markets_a: list[Market],
    markets_b: list[Market],
    *,
    name_threshold: float = 0.6,
    min_shared: int = 3,
    top_k: int = 20,
) -> list[EventPairSuggestion]:
    """Match multi-outcome markets by contestant name, then group by event pair.

    A Kalshi event and a Polymarket event that share many matched contestant
    names are very likely the same market family (still human-verified — the two
    events may differ, e.g. 'general election' vs 'nomination').
    """
    a_out = [m for m in markets_a if _subtitle(m)]
    b_out = [m for m in markets_b if _subtitle(m)]

    groups: dict[tuple, dict[str, OutcomeMatch]] = {}
    titles: dict[tuple, tuple[str, str]] = {}
    for a in a_out:
        an = _subtitle(a) or ""
        for b in b_out:
            score = _name_similarity(an, _subtitle(b) or "")
            if score < name_threshold:
                continue
            key = (a.series_key, b.series_key)
            best = groups.setdefault(key, {})
            titles.setdefault(key, (a.title, b.title))
            # Keep the best Polymarket match per Kalshi market.
            prev = best.get(a.market_id)
            if prev is None or score > prev.score:
                best[a.market_id] = OutcomeMatch(score=score, a=a, b=b)

    out: list[EventPairSuggestion] = []
    for key, best in groups.items():
        matches = sorted(best.values(), key=lambda m: m.score, reverse=True)
        if len(matches) >= min_shared:
            ta, tb = titles[key]
            out.append(EventPairSuggestion(key[0], key[1], ta, tb, matches))
    out.sort(key=lambda e: e.shared, reverse=True)
    return out[:top_k]


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def render_pairs_yaml(evp: EventPairSuggestion) -> str:
    """Ready-to-paste markets.yaml `pairs:` entries for one event pair.

    Aligns both venues' YES = 'this contestant wins'. VERIFY the two events are
    truly the same resolution before trading (general election vs nomination,
    etc.) — this is a suggestion, not a confirmed mapping.
    """
    base = _slug(evp.series_b or evp.series_a or "pair")
    header = (
        f"# Kalshi {evp.series_a}  <->  Polymarket {evp.series_b}  "
        f"({evp.shared} shared) — VERIFY same event before trading"
    )
    lines = [header, "pairs:"]
    for m in evp.matches:
        name = m.name
        lines += [
            f"  - canonical_id: {base}-{_slug(name)}",
            f"    description: \"{name}\"",
            "    legs:",
            "      kalshi:",
            f"        market_id: {m.a.market_id}",
            "        outcome: yes",
            "      polymarket:",
            f"        market_id: \"{m.b.market_id}\"",
            "        outcome: yes",
        ]
    return "\n".join(lines)
