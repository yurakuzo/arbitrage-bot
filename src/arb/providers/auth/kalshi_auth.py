"""Kalshi API request signing (RSA-PSS / SHA-256).

Kalshi authenticates each request with headers:
    KALSHI-ACCESS-KEY        = API key id
    KALSHI-ACCESS-TIMESTAMP  = current time in milliseconds
    KALSHI-ACCESS-SIGNATURE  = base64( RSA-PSS-SHA256( timestamp + METHOD + path ) )

The signed `path` excludes the query string. `cryptography` is only needed for
live trading, so it is an optional (`.[live]`) dependency and imported lazily with
a clear message if absent.
"""

from __future__ import annotations

import base64
import time
from pathlib import Path


class KalshiSigner:
    def __init__(self, key_id: str, private_key_pem: bytes | str, password: bytes | None = None):
        try:
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import padding
        except ImportError as exc:  # pragma: no cover - depends on optional extra
            raise ImportError(
                "Kalshi live auth needs 'cryptography'. Install with: pip install -e '.[live]'"
            ) from exc

        self._hashes = hashes
        self._padding = padding
        if isinstance(private_key_pem, str):
            private_key_pem = private_key_pem.encode()
        self.key_id = key_id
        self._key = serialization.load_pem_private_key(private_key_pem, password=password)

    @classmethod
    def from_file(cls, key_id: str, path: str | Path, password: bytes | None = None) -> KalshiSigner:
        return cls(key_id, Path(path).read_bytes(), password=password)

    def sign(self, method: str, path: str, timestamp_ms: int | None = None) -> tuple[str, str]:
        """Return (timestamp_str, base64_signature) for `method` + `path`."""
        ts = str(timestamp_ms if timestamp_ms is not None else int(time.time() * 1000))
        message = (ts + method.upper() + path).encode()
        signature = self._key.sign(
            message,
            self._padding.PSS(
                mgf=self._padding.MGF1(self._hashes.SHA256()),
                salt_length=self._padding.PSS.DIGEST_LENGTH,
            ),
            self._hashes.SHA256(),
        )
        return ts, base64.b64encode(signature).decode()

    def headers(self, method: str, path: str, timestamp_ms: int | None = None) -> dict[str, str]:
        ts, sig = self.sign(method, path, timestamp_ms)
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": sig,
        }
