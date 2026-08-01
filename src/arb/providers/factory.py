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
    venues: list[str],
    environment: str = "demo",
    settings: Settings | None = None,
    polymarket_wallet_env: str | None = None,
) -> dict[str, Provider]:
    """Instantiate providers. When `settings` is given, live credentials are
    threaded to the venues that need them (Kalshi RSA key; Polymarket wallet env).
    `polymarket_wallet_env` overrides the wallet file for this run."""
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
            providers[key] = PolymarketProvider(
                environment=environment,
                wallet_env_file=polymarket_wallet_env
                or (settings.polymarket_env_file if settings else None),
                signature_type=settings.polymarket_signature_type if settings else 2,
            )
        else:
            raise ValueError(f"Unknown venue '{name}'. Known: {sorted(_REGISTRY)}")
    return providers
