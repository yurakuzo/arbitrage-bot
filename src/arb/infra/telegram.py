"""Telegram notifier (Bot API).

Used for anomaly/error alerts from the live engine. Fails soft: if Telegram is
not configured or the send fails, we log and continue rather than crashing the
bot on a notification problem.
"""

from __future__ import annotations

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
