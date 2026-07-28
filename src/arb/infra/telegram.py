"""Telegram notifier (Bot API).

Used for anomaly/error alerts from the live engine. Fails soft: if Telegram is
not configured or the send fails, we log and continue rather than crashing the
bot on a notification problem.
"""

from __future__ import annotations

import asyncio
import secrets
import time

import httpx

from arb.infra.logging import get_logger

log = get_logger(__name__)

_API = "https://api.telegram.org"


class TelegramNotifier:
    def __init__(self, bot_token: str | None, chat_id: str | None):
        self.bot_token = bot_token
        self.chat_id = chat_id

    @property
    def enabled(self) -> bool:
        return bool(self.bot_token and self.chat_id)

    async def send(self, text: str) -> bool:
        """Send a message. Returns True on success, False otherwise (never raises)."""
        if not self.enabled:
            log.warning("Telegram not configured; dropping alert: %s", text)
            return False
        url = f"{_API}/bot{self.bot_token}/sendMessage"
        payload = {"chat_id": self.chat_id, "text": text, "parse_mode": "HTML"}
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(url, json=payload)
                resp.raise_for_status()
            return True
        except Exception as exc:  # noqa: BLE001 — notifier must never crash caller
            log.error("Telegram send failed: %s", exc)
            return False

    async def confirm(self, prompt: str, timeout_s: int = 120) -> bool:
        """Semi-auto gate: send `prompt` and wait for an affirmative reply.

        A random nonce is embedded; the user must reply `yes <nonce>` to approve
        or `no <nonce>` to reject. Times out to False (safe default: do nothing).
        Never raises — any error resolves to False (no trade).
        """
        if not self.enabled:
            log.warning("Telegram not configured; cannot confirm — treating as NO.")
            return False
        nonce = secrets.token_hex(3)
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                # Baseline: ignore any pre-existing updates.
                base = await client.get(f"{_API}/bot{self.bot_token}/getUpdates")
                offset = max((u["update_id"] for u in base.json().get("result", [])), default=0) + 1

                await self.send(
                    f"{prompt}\n\nReply <code>yes {nonce}</code> to APPROVE or "
                    f"<code>no {nonce}</code> to reject (within {timeout_s}s)."
                )
                deadline = time.monotonic() + timeout_s
                while time.monotonic() < deadline:
                    resp = await client.get(
                        f"{_API}/bot{self.bot_token}/getUpdates",
                        params={"offset": offset, "timeout": 10},
                    )
                    for u in resp.json().get("result", []):
                        offset = u["update_id"] + 1
                        text = ((u.get("message") or {}).get("text") or "").strip().lower()
                        if nonce not in text:
                            continue
                        if text.startswith("yes"):
                            return True
                        if text.startswith("no"):
                            return False
                    await asyncio.sleep(1)
        except Exception as exc:  # noqa: BLE001 — confirmation failure => no trade
            log.error("Telegram confirm failed: %s", exc)
        return False
