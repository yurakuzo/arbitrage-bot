"""Shared async HTTP helpers for REST providers.

Thin wrapper around httpx.AsyncClient with:
- optional client-side rate limiting (so we don't hammer a venue into 429s), and
- retry/backoff on transient errors (429 / 5xx / network).

Keeps provider code focused on payload shapes.
"""

from __future__ import annotations

import asyncio
import time

import httpx

from arb.infra.logging import get_logger

log = get_logger(__name__)


class RestClient:
    def __init__(
        self,
        base_url: str = "",
        timeout: float = 20.0,
        max_retries: int = 4,
        rate_per_sec: float | None = None,
    ):
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": "arbitrage-bot/0.1"},
        )
        self.max_retries = max_retries
        # Spread requests at least this far apart (0 = no limit). Enforced across
        # all concurrent callers via a lock + next-slot clock.
        self._min_interval = 1.0 / rate_per_sec if rate_per_sec else 0.0
        self._next_at = 0.0
        self._rate_lock = asyncio.Lock()

    async def _throttle(self) -> None:
        if self._min_interval <= 0:
            return
        async with self._rate_lock:
            now = time.monotonic()
            slot = max(now, self._next_at)
            self._next_at = slot + self._min_interval
        delay = slot - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)

    async def get_json(self, url: str, params: dict | None = None) -> dict | list:
        last_exc: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            await self._throttle()
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
                # Honor Retry-After on 429 when present, else exponential backoff.
                backoff = 0.5 * (2 ** (attempt - 1))
                retry_after = getattr(getattr(exc, "response", None), "headers", {})
                if retry_after and retry_after.get("retry-after", "").isdigit():
                    backoff = max(backoff, float(retry_after["retry-after"]))
                # Retries are expected under load — keep them at DEBUG, not WARNING.
                log.debug("GET %s failed (%s); retry %d in %.1fs", url, exc, attempt, backoff)
                await asyncio.sleep(backoff)
        assert last_exc is not None
        raise last_exc

    async def aclose(self) -> None:
        await self._client.aclose()
