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
def collect(
    limit: int = typer.Option(200, help="Max markets to snapshot per venue."),
    venue: str | None = typer.Option(None, help="Only collect this venue (else all configured)."),
) -> None:
    """[Phase 1] Sweep markets across venues and snapshot into the DB."""
    from arb.analysis.collector import run_collection
    from arb.providers.factory import build_providers

    s = get_settings()
    cfg = load_app_config()
    venues = [venue] if venue else cfg.venues
    providers = build_providers(venues, environment=s.environment)
    db = Database(s.db_file)
    discovery = cfg.discovery.model_dump()
    counts = asyncio.run(run_collection(providers, db, limit, discovery))
    for name, n in counts.items():
        rprint(f"[green]{name}[/green]: {n} snapshots")


@app.command()
def report(
    out: str = typer.Option("reports/analysis.xlsx", help="Output .xlsx path."),
) -> None:
    """[Phase 1] Export an Excel analysis report from collected snapshots."""
    from arb.analysis.report import write_report

    s = get_settings()
    path = write_report(Database(s.db_file), out)
    rprint(f"[green]Report written:[/green] {path}")


@app.command()
def suggest(
    threshold: float = typer.Option(0.25, help="Min title-similarity score (0-1)."),
    top_k: int = typer.Option(30, help="Max suggestions to show."),
) -> None:
    """[Phase 2] Propose candidate cross-venue matches (for review, not confirmed)."""
    from arb.core.matching import suggest_matches
    from arb.providers.models import Venue

    s = get_settings()
    db = Database(s.db_file)
    # Reuse collected market metadata as the candidate universe.
    import sqlite3

    from arb.providers.models import Market

    def _load(conn, venue: str) -> list[Market]:
        rows = conn.execute(
            "SELECT venue, market_id, title, series_key FROM markets WHERE venue = ?", (venue,)
        ).fetchall()
        return [Market(venue=Venue(r[0]), market_id=r[1], title=r[2] or "", series_key=r[3]) for r in rows]

    with db.transaction() as conn:
        conn.row_factory = sqlite3.Row
        ks = _load(conn, "kalshi")
        ps = _load(conn, "polymarket")
    suggestions = suggest_matches(ks, ps, threshold=threshold, top_k=top_k)
    if not suggestions:
        rprint("[yellow]No candidate matches above threshold. Try lowering --threshold.[/yellow]")
        return
    rprint(f"[bold]{len(suggestions)} candidate match(es)[/bold] (review before adding to markets.yaml):\n")
    for sug in suggestions:
        rprint(f"[green]{sug.score:.2f}[/green] terms={sug.shared_terms}")
        rprint(f"   kalshi     [{sug.a.market_id}] {sug.a.title[:70]}")
        rprint(f"   polymarket [{sug.b.market_id[:18]}…] {sug.b.title[:70]}\n")


@app.command()
def shortlist(
    mappings: str = typer.Option("config/markets.yaml", help="Curated mappings file."),
    out: str = typer.Option("reports/shortlist.xlsx", help="Output .xlsx path."),
) -> None:
    """[Phase 2] Rank curated cross-venue pairs by fee-net edge."""
    from arb.analysis.shortlist import write_shortlist

    s = get_settings()
    df = write_shortlist(Database(s.db_file), mappings, out)
    if df.empty:
        rprint("[yellow]No priced pairs. Add entries to markets.yaml and run `arb collect` first.[/yellow]")
        return
    rprint(f"[green]Shortlist written:[/green] {out}  ({len(df)} pairs)")
    for _, r in df.head(10).iterrows():
        flag = "[green]+[/green]" if r["net_edge_per_contract"] > 0 else "[red]-[/red]"
        rprint(
            f"  {flag} {r['canonical_id']}: net edge/contract="
            f"{r['net_edge_per_contract']:.4f}  ({r['buy_a']} @ {r['price_a']} + {r['buy_b']} @ {r['price_b']})"
        )


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
