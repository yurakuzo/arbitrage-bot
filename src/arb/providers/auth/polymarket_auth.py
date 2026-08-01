"""Polymarket wallet credentials for live execution (Phase 4b).

Each wallet lives in its own dotenv file (e.g. `.env.1pixel`) with at least:
    PRIVATE_KEY      = 0x...        # the EOA signing key
    PROXY_WALLET     = 0x...        # Polymarket proxy/funder that holds USDC
    CLOB_HTTP_URL    = https://clob.polymarket.com   (optional)
    SIGNATURE_TYPE   = 1 | 2        (optional; matches the wallet type)

Credentials are read with `dotenv_values` (which does NOT touch `os.environ`), so
secrets stay scoped to the provider instance. `__repr__` redacts the key so it can
never be logged or printed by accident.

Signature type: Polymarket email/magic wallets use type 1 (POLY_PROXY); browser
(Gnosis Safe) wallets use type 2 (POLY_GNOSIS_SAFE). Set SIGNATURE_TYPE to match
the wallet your PRIVATE_KEY/PROXY_WALLET pair belongs to.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values

DEFAULT_HOST = "https://clob.polymarket.com"
CHAIN_ID_POLYGON = 137


@dataclass(frozen=True)
class PolymarketCredentials:
    private_key: str
    proxy_wallet: str | None
    signature_type: int
    host: str
    chain_id: int = CHAIN_ID_POLYGON

    @classmethod
    def from_env_file(cls, path: str | Path, default_signature_type: int = 2) -> PolymarketCredentials:
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"Polymarket wallet env file not found: {p}")
        vals = dotenv_values(p)
        pk = vals.get("PRIVATE_KEY") or vals.get("ARB_POLYMARKET_PRIVATE_KEY")
        if not pk:
            raise ValueError(f"No PRIVATE_KEY found in {p}")
        sig_raw = vals.get("SIGNATURE_TYPE")
        return cls(
            private_key=pk,
            proxy_wallet=vals.get("PROXY_WALLET") or vals.get("USER_ADDRESS"),
            signature_type=int(sig_raw) if sig_raw else default_signature_type,
            host=vals.get("CLOB_HTTP_URL") or DEFAULT_HOST,
        )

    def __repr__(self) -> str:  # never leak the key
        who = self.proxy_wallet or "?"
        return (
            f"PolymarketCredentials(private_key=***redacted***, proxy_wallet={who}, "
            f"signature_type={self.signature_type}, host={self.host})"
        )
