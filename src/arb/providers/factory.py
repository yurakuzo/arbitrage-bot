"""Build provider instances from config.

The single place that maps venue names -> concrete Provider classes. Core code
asks for providers by name; adding a venue means registering it here.
"""

from __future__ import annotations

from arb.providers.base import Provider
from arb.providers.kalshi import KalshiProvider
from arb.providers.polymarket import PolymarketProvider

_REGISTRY = {
    "kalshi": KalshiProvider,
    "polymarket": PolymarketProvider,
}


def build_providers(venues: list[str], environment: str = "demo") -> dict[str, Provider]:
    providers: dict[str, Provider] = {}
    for name in venues:
        cls = _REGISTRY.get(name.lower())
        if cls is None:
            raise ValueError(f"Unknown venue '{name}'. Known: {sorted(_REGISTRY)}")
        providers[name.lower()] = cls(environment=environment)
    return providers
