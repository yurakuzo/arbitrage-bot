"""Shared async HTTP helpers for REST providers.

Thin wrapper around httpx.AsyncClient with retry/backoff on transient errors
(429 / 5xx / network). Keeps provider code focused on payload shapes.
"""

from __future__ import annotations

import asyncio

import httpx

from arb.infra.logging import get_logger

log = get_logger(__name__)


class RestClient:
    def __init__(self, base_url: str = "", timeout: float = 20.0, max_retries: int = 3):
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": "arbitrage-bot/0.1"},
        )
        self.max_retries = max_retries

    async def get_json(self, url: str, params: dict | None = None) -> dict | list:
        last_exc: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = await self._client.get(url, params=params)
                if resp.status_code == 429 or resp.status_code >= 500:
                    raise httpx.HTTPStatusError(
                        f"retryable {resp.status_code}", request=resp.request, response=resp
                    )
                resp.raise_for_status()
                return resp.json()
            except (httpx.HTTPStatusError, httpx.TransportError) as exc:
                last_exc = exc
                if attempt == self.max_retries:
                    break
                backoff = 0.5 * (2 ** (attempt - 1))
                log.warning("GET %s failed (%s); retry %d in %.1fs", url, exc, attempt, backoff)
                await asyncio.sleep(backoff)
        assert last_exc is not None
        raise last_exc

    async def aclose(self) -> None:
        await self._client.aclose()
