"""Command-line entrypoint: `arb <command>`.

Phase 0 wires the skeleton: `init-db`, `check`, and `setup-telegram` are live;
`collect`, `report`, and `run` are stubs filled in by later phases.
"""

from __future__ import annotations

import asyncio

import typer
from rich import print as rprint

from arb.config import get_settings, load_app_config
from arb.infra.db import Database
from arb.infra.logging import setup_logging
from arb.infra.telegram import TelegramNotifier

app = typer.Typer(add_completion=False, help="Polymarket/Kalshi arbitrage bot (v1: paper-trading).")


@app.callback()
def _root() -> None:
    setup_logging()


@app.command()
def check() -> None:
    """Print the resolved config and which integrations are ready."""
    s = get_settings()
    cfg = load_app_config()
    rprint(f"[bold]Environment:[/bold] {s.environment}")
    rprint(f"[bold]DB path:[/bold] {s.db_file}")
    rprint(f"[bold]Telegram configured:[/bold] {s.telegram_enabled}")
    rprint(f"[bold]Kalshi key set:[/bold] {bool(s.kalshi_api_key_id)}")
    rprint(f"[bold]Venues:[/bold] {', '.join(cfg.venues)}")
    rprint(f"[bold]Thresholds:[/bold] {cfg.thresholds.model_dump()}")


@app.command("init-db")
def init_db() -> None:
    """Create the SQLite schema."""
    s = get_settings()
    db = Database(s.db_file)
    db.init_schema()
    rprint(f"[green]Initialized database at[/green] {s.db_file}")


@app.command("setup-telegram")
def setup_telegram() -> None:
    """Interactively discover your chat ID and send a test message.

    Prerequisite: create a bot via @BotFather and put its token in .env as
    ARB_TELEGRAM_BOT_TOKEN. See docs/PROJECT.md -> 'Step: Telegram bot setup'.
    """
    from arb.tools.telegram_setup import run

    asyncio.run(run())


@app.command()
def collect() -> None:
    """[Phase 1] Sweep markets across venues and snapshot into the DB."""
    rprint("[yellow]collect[/yellow] is implemented in Phase 1 (analysis mode).")


@app.command()
def report() -> None:
    """[Phase 1] Export an Excel analysis report from collected snapshots."""
    rprint("[yellow]report[/yellow] is implemented in Phase 1 (analysis mode).")


@app.command()
def run() -> None:
    """[Phase 3] Run the live paper-trading engine over shortlisted markets."""
    rprint("[yellow]run[/yellow] is implemented in Phase 3 (live paper engine).")


@app.command("test-alert")
def test_alert() -> None:
    """Send a test Telegram alert to verify notifications work end-to-end."""
    s = get_settings()
    notifier = TelegramNotifier(s.telegram_bot_token, s.telegram_chat_id)
    ok = asyncio.run(notifier.send("✅ arbitrage-bot test alert"))
    rprint("[green]Sent.[/green]" if ok else "[red]Failed — see logs / run `arb check`.[/red]")


if __name__ == "__main__":
    app()
