"""Build provider instances from config.

The single place that maps venue names -> concrete Provider classes. Core code
asks for providers by name; adding a venue means registering it here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from arb.providers.base import Provider
from arb.providers.kalshi import KalshiProvider
from arb.providers.polymarket import PolymarketProvider

if TYPE_CHECKING:
    from arb.config import Settings

_REGISTRY = {
    "kalshi": KalshiProvider,
    "polymarket": PolymarketProvider,
}


def build_providers(
    venues: list[str], environment: str = "demo", settings: Settings | None = None
) -> dict[str, Provider]:
    """Instantiate providers. When `settings` is given, live credentials are
    threaded to the venues that need them (Kalshi RSA key for Phase 4)."""
    providers: dict[str, Provider] = {}
    for name in venues:
        key = name.lower()
        if key == "kalshi":
            providers[key] = KalshiProvider(
                environment=environment,
                key_id=settings.kalshi_api_key_id if settings else None,
                private_key_path=settings.kalshi_private_key_path if settings else None,
            )
        elif key == "polymarket":
            providers[key] = PolymarketProvider(environment=environment)
        else:
            raise ValueError(f"Unknown venue '{name}'. Known: {sorted(_REGISTRY)}")
    return providers
