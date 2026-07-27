"""Interactive helper to obtain a Telegram chat ID and verify notifications.

Flow (see docs/PROJECT.md -> 'Step: Telegram bot setup'):
  1. You create a bot with @BotFather and copy its token into .env
     (ARB_TELEGRAM_BOT_TOKEN).
  2. You open your bot in Telegram and send it any message (e.g. "hi").
  3. This helper calls getUpdates, extracts the chat ID, prints the line to add
     to .env, and sends a confirmation message.

This never writes .env for you — it prints the value so you stay in control of
your secrets across machines.
"""

from __future__ import annotations

import httpx
from rich import print as rprint

from arb.config import get_settings

_API = "https://api.telegram.org"


async def run() -> None:
    s = get_settings()
    token = s.telegram_bot_token
    if not token:
        rprint("[red]ARB_TELEGRAM_BOT_TOKEN is not set in .env.[/red]")
        rprint("Create a bot via @BotFather, then add the token to .env and retry.")
        return

    async with httpx.AsyncClient(timeout=15) as client:
        # If chat id already known, just send a test.
        if s.telegram_chat_id:
            rprint(f"Chat ID already set: [cyan]{s.telegram_chat_id}[/cyan]. Sending test…")
            await _send(client, token, s.telegram_chat_id, "✅ arbitrage-bot: Telegram configured.")
            rprint("[green]Test message sent.[/green]")
            return

        rprint("[bold]Open your bot in Telegram and send it any message, then rerun if empty.[/bold]")
        resp = await client.get(f"{_API}/bot{token}/getUpdates")
        resp.raise_for_status()
        updates = resp.json().get("result", [])
        chat_ids = _extract_chat_ids(updates)

        if not chat_ids:
            rprint("[yellow]No messages found.[/yellow] Send your bot a message, then rerun.")
            return

        chat_id = chat_ids[-1]
        rprint("Discovered chat ID(s):", chat_ids)
        rprint(f"\nAdd this line to your [bold].env[/bold]:\n  [cyan]ARB_TELEGRAM_CHAT_ID={chat_id}[/cyan]\n")
        await _send(client, token, str(chat_id), "✅ arbitrage-bot: chat linked.")
        rprint("[green]Confirmation message sent to that chat.[/green]")


def _extract_chat_ids(updates: list[dict]) -> list[int]:
    ids: list[int] = []
    for u in updates:
        msg = u.get("message") or u.get("channel_post") or {}
        chat = msg.get("chat") or {}
        cid = chat.get("id")
        if cid is not None and cid not in ids:
            ids.append(cid)
    return ids


async def _send(client: httpx.AsyncClient, token: str, chat_id: str, text: str) -> None:
    await client.post(
        f"{_API}/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text},
    )
