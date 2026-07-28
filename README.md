# arbitrage-bot

[![CI](https://github.com/yurakuzo/arbitrage-bot/actions/workflows/ci.yml/badge.svg)](https://github.com/yurakuzo/arbitrage-bot/actions/workflows/ci.yml)

Cross-venue arbitrage bot for **Polymarket** and **Kalshi**.

- **Analysis mode** — cron sweep of repeatable markets → SQLite → Excel report.
- **Live mode** — WebSocket feeds → real-time fee-net edge detection & optimal
  cross-venue sizing → paper-trading + Telegram alerts.

> **v1 is detection + paper-trading only — it never places real orders.**
> Live execution is a later, explicitly config-gated phase.

📖 **Full docs, decisions, API reference, and setup:** [`docs/PROJECT.md`](docs/PROJECT.md)

## Quick start

```bash
python -m venv .venv; .venv\Scripts\Activate.ps1
pip install -e ".[dev]"
copy .env.example .env          # fill in secrets
arb init-db
arb check
pytest
```

See [`docs/PROJECT.md` → Setup](docs/PROJECT.md#6-setup) for the Telegram bot step
and everything else.
