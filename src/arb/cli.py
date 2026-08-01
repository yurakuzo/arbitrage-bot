"""Command-line entrypoint: `arb <command>`.

Phase 0 wires the skeleton: `init-db`, `check`, and `setup-telegram` are live;
`collect`, `report`, and `run` are stubs filled in by later phases.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

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
    wallet = s.polymarket_env_file
    missing = " [red](missing!)[/red]" if wallet and not Path(wallet).exists() else ""
    rprint(f"[bold]Polymarket wallet env:[/bold] {wallet or '(not set)'}{missing}")
    rprint(f"[bold]Venues:[/bold] {', '.join(cfg.venues)}")
    rprint(f"[bold]Thresholds:[/bold] {cfg.thresholds.model_dump()}")
    from arb.live.gate import TradingGate

    gate = TradingGate.from_config(s, cfg.execution)
    colour = "red" if gate.places_real_orders else "green"
    rprint(f"[bold]Trading gate:[/bold] [{colour}]{gate.describe()}[/{colour}]")


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
def run(
    mappings: str = typer.Option("config/markets.yaml", help="Curated mappings file."),
    duration: float | None = typer.Option(
        None, help="Stop after N seconds (default: run until Ctrl+C)."
    ),
    poll: bool = typer.Option(False, "--poll", help="Force REST polling for all venues (no WS)."),
    mode: str | None = typer.Option(
        None, help="Override execution mode: paper | semi_auto | auto (default: config)."
    ),
    wallet_env: str | None = typer.Option(
        None, help="Polymarket wallet dotenv file for live orders (e.g. .env.1pixel)."
    ),
) -> None:
    """[Phase 3/4] Run the live engine over curated pairs.

    Detects fee-net arbitrage in real time and sends Telegram alerts. Execution
    mode (config or --mode) decides what happens on a hit: paper simulates;
    semi_auto asks for Telegram confirmation; auto places directly. Real orders
    require the full opt-in (see `arb check` → trading gate) — otherwise it
    transparently falls back to paper.
    """
    from arb.core.matching import load_mappings
    from arb.infra.anomaly import AnomalyReporter
    from arb.live.engine import Engine
    from arb.live.executor import Executor
    from arb.live.gate import TradingGate
    from arb.live.simulator import PaperLedger
    from arb.providers.factory import build_providers

    s = get_settings()
    cfg = load_app_config()
    exec_mode = mode or cfg.execution.mode
    pairs = load_mappings(mappings)
    if not pairs:
        rprint(f"[yellow]No mappings in {mappings}. Add pairs (see markets.example.yaml) first.[/yellow]")
        return

    db = Database(s.db_file)
    db.init_schema()
    venues = sorted({v for p in pairs for v in p.legs})
    providers = build_providers(
        [v for v in venues if v in cfg.venues],
        environment=s.environment,
        settings=s,
        polymarket_wallet_env=wallet_env,
    )
    notifier = TelegramNotifier(s.telegram_bot_token, s.telegram_chat_id)
    gate = TradingGate.from_config(s, cfg.execution)
    executor = Executor(
        mode=exec_mode,
        providers=providers,
        notifier=notifier,
        ledger=PaperLedger(db=db),
        gate=gate,
        execution=cfg.execution,
        anomaly=AnomalyReporter(notifier),
    )
    engine = Engine(
        providers=providers,
        pairs=pairs,
        executor=executor,
        anomaly=AnomalyReporter(notifier),
        thresholds=cfg.thresholds,
    )
    banner = "LIVE" if gate.places_real_orders else "paper"
    rprint(f"[bold]Engine[/bold] mode=[cyan]{exec_mode}[/cyan] gate=[cyan]{banner}[/cyan] "
           f"— {len(pairs)} pair(s); Ctrl+C to stop.")
    if gate.places_real_orders:
        rprint("[red bold]⚠ REAL ORDERS ENABLED — this can spend real money.[/red bold]")
    try:
        asyncio.run(engine.run(duration_s=duration, force_poll=poll))
    except KeyboardInterrupt:
        rprint("\n[yellow]Stopped.[/yellow]")
    rprint(f"[green]Trades:[/green] {executor.ledger.trades}  "
           f"[green]P&L:[/green] ${executor.ledger.realized_profit:.2f}")


@app.command()
def pnl(
    limit: int = typer.Option(10, help="How many recent trades to list."),
    out: str | None = typer.Option(None, help="Also export the full breakdown to this .xlsx."),
) -> None:
    """Summarize the paper-trade ledger: net P&L, per-pair and per-venue breakdown."""
    from rich.table import Table

    from arb.analysis.pnl import summarize

    s = get_settings()
    db = Database(s.db_file)
    summ = summarize(db, recent_limit=limit)
    if summ.is_empty:
        rprint("[yellow]No paper trades recorded yet. Run `arb run` first.[/yellow]")
        return

    colour = "green" if summ.net_profit >= 0 else "red"
    rprint(
        f"[bold]Paper P&L[/bold]  trades=[cyan]{summ.trades}[/cyan]  "
        f"contracts=[cyan]{summ.contracts:.0f}[/cyan]  "
        f"cost=${summ.gross_cost:.2f}  fees=${summ.fees:.2f}  "
        f"net=[{colour}]${summ.net_profit:.2f}[/{colour}]  ROI={summ.roi * 100:.2f}%"
    )

    pair_tbl = Table(title="By pair", show_edge=False)
    pair_tbl.add_column("canonical_id"); pair_tbl.add_column("trades", justify="right")
    pair_tbl.add_column("net $", justify="right")
    for _, r in summ.by_pair.iterrows():
        pair_tbl.add_row(str(r["canonical_id"]), str(int(r["trades"])), f"{r['net_profit']:.2f}")
    rprint(pair_tbl)

    venue_tbl = Table(title="By venue", show_edge=False)
    for col in ("venue", "legs", "contracts", "fees $", "cost $"):
        venue_tbl.add_column(col, justify="right" if col != "venue" else "left")
    for _, r in summ.by_venue.iterrows():
        venue_tbl.add_row(str(r["venue"]), str(int(r["legs"])), f"{r['contracts']:.0f}",
                          f"{r['fees']:.2f}", f"{r['cost']:.2f}")
    rprint(venue_tbl)

    if out:
        from arb.analysis.pnl import export_excel

        path = export_excel(db, out)
        rprint(f"[green]P&L workbook written:[/green] {path}")


@app.command("test-alert")
def test_alert() -> None:
    """Send a test Telegram alert to verify notifications work end-to-end."""
    s = get_settings()
    notifier = TelegramNotifier(s.telegram_bot_token, s.telegram_chat_id)
    ok = asyncio.run(notifier.send("✅ arbitrage-bot test alert"))
    rprint("[green]Sent.[/green]" if ok else "[red]Failed — see logs / run `arb check`.[/red]")


if __name__ == "__main__":
    app()
