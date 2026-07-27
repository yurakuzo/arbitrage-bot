# Arbitrage Bot — Polymarket & Kalshi

Single source of truth for this project. Committed to the repo so it stays in sync
across machines. Covers the goal, decisions, architecture, API reference, setup
(including Telegram), and the development roadmap.

---

## 1. Goal

Exploit positive price differences between two prediction-market venues
(**Polymarket** and **Kalshi**) on equivalent binary outcomes. Both venues price
YES/NO contracts that settle at $0 or $1 (Polymarket in USDC on Polygon; Kalshi in
USD, cents 1–99). For the same real-world outcome, if

```
buy_YES @ venue A  +  buy_NO @ venue B   <   $1.00   (net of all fees & slippage)
```

then a profit is locked in regardless of how the event resolves. The bot finds and
sizes these.

## 2. Two modes

1. **Analysis mode** — a cron-driven sweep of popular, *repeatable* markets
   (recurring weather, economic prints, etc.). Snapshots go into SQLite; a report
   step exports an **Excel** table with the stats needed to shortlist which markets
   are worth trading. Seed markets are chosen *after* this analysis, not up front.
2. **Live bot mode** — a long-running async process holding **WebSocket** order-book
   feeds from both venues for shortlisted markets. In real time it computes the
   **optimal cross-venue bet sizes** for a net-positive arbitrage, accounting for
   **fees, order-book depth/volume, and volatility**. On any error/anomaly it
   degrades gracefully **and sends a Telegram alert**.

Both modes sit on one **pluggable Provider interface** so new venues plug in by
implementing a single class.

## 3. Decisions (locked)

| Decision | Choice | Why |
|---|---|---|
| Language | **Python 3.11+, asyncio** | pandas/openpyxl for Excel; both venues have Python SDKs; strong async. |
| v1 execution | **Detect + paper-trade only** | Validate strategy with zero money at risk. `place_order` raises `NotImplementedError`. |
| Storage | **SQLite** time-series + **Excel** export | No infra; enough for cross-run statistics. |
| Structure | **One repo, shared core** | Analysis CLI and live bot reuse the same providers/models. |
| Market matching | **Curated** (`config/markets.yaml`) + heuristic *suggestions* | A false match manufactures fake arb → real loss. No fully-automatic matching in v1. |

### ⚠️ Regulatory constraint (design-shaping, not legal advice)
Polymarket **blocks US persons** (geoblocked; VPN circumvention prohibited by ToS).
Kalshi is a **US CFTC-regulated** venue (with active 2026 state-level litigation).
Legally holding funded accounts on both from one jurisdiction is the central
compliance question — **the user's decision, not the bot's**. v1 is detection +
simulation, which sidesteps execution entirely. Live trading (Phase 4) is gated
behind an explicit config flag; the bot must never place a real order without it.

## 4. Architecture

```
arbitrage-bot/
  pyproject.toml
  config/
    config.example.yaml   # non-secret tuning (thresholds, venues) -> copy to config.yaml
    markets.yaml          # (Phase 2) canonical cross-venue market mapping
  src/arb/
    config.py             # Settings (.env secrets) + AppConfig (yaml tuning)
    cli.py                # `arb` entrypoint: check / init-db / setup-telegram / collect / report / run
    providers/
      base.py             # Provider ABC — the pluggable interface
      models.py           # normalized Venue/Outcome/PriceLevel/OutcomeBook/MarketBook/Market
      kalshi.py           # (Phase 1) REST discovery + books; WS; RSA-PSS auth
      polymarket.py       # (Phase 1) Gamma discovery + CLOB books; WS
    core/
      fees.py             # per-venue fee models (Kalshi convex; Polymarket bps)  [done]
      pricing.py          # (Phase 3) fee-net cross-venue edge math
      sizing.py           # (Phase 3) book-walking optimal stake allocation
      matching.py         # (Phase 2) equivalent-market mapping + suggestions
    analysis/
      collector.py        # (Phase 1) cron entrypoint: sweep -> snapshot -> DB
      report.py           # (Phase 1) DB -> Excel
    live/
      engine.py           # (Phase 3) feeds -> detect -> size -> paper-exec
      feeds.py            # (Phase 3) WS managers w/ reconnect + delta resync
      simulator.py        # (Phase 3) paper fills + P&L ledger
    infra/
      db.py               # SQLite schema + access                              [done]
      telegram.py         # Bot API notifier (fails soft)                        [done]
      anomaly.py          # error/anomaly detection -> Telegram                  [done]
      logging.py          # rich logging                                         [done]
    tools/
      telegram_setup.py   # interactive chat-ID discovery + test message         [done]
  scripts/setup_telegram.py
  tests/                  # fee + smoke tests                                    [done]
```

### The Provider interface (`src/arb/providers/base.py`)
Core logic never imports a venue directly.
- `list_markets(filter)` — enumerate markets (analysis).
- `get_order_book(market_id)` — REST snapshot.
- `stream_order_books(market_ids)` — async iterator of live WS updates.
- `fee_model(series_key)` — venue fee model.
- `place_order` / `cancel_order` — **raise `NotImplementedError` in v1**.

### Normalized price convention
All prices are floats in **dollars [0, 1]**. Kalshi cents (1–99) and Polymarket
USDC prices are converted at the provider boundary. `OutcomeBook.asks` is always
"the ladder you consume to BUY this outcome, cheapest first".

## 5. Venue API reference (verified 2026-07)

### Kalshi (custodial, USD, no crypto)
- REST prod `https://api.elections.kalshi.com/trade-api/v2`; **demo**
  `https://demo-api.kalshi.co/trade-api/v2` — **develop against demo first**.
- Discovery: `GET /series | /events | /markets`. Book: `GET /markets/{ticker}/orderbook`.
- WS `wss://api.elections.kalshi.com/trade-api/ws/v2`; channel `orderbook_delta`
  (snapshot then incremental deltas — must maintain book locally & resync on gaps),
  plus `ticker`, `trade`.
- Auth: API key ID + **RSA-PSS/SHA-256** signature over `timestamp(ms)+METHOD+path`
  → headers `KALSHI-ACCESS-KEY / -TIMESTAMP / -SIGNATURE`.
- Orders (Phase 4): `POST /portfolio/orders`; prices in cents (1–99).
- **Fee (verified):** `ceil(0.07 · C · P · (1−P))` rounded up to the next cent →
  peaks **$1.75 / 100 contracts at P=0.50**. Per-series multipliers exist.
- SDK: `kalshi_python_sync` / `kalshi_python_async` (old `kalshi-python` deprecated).
- Rate limits: token bucket, separate read/write; Basic tier 200 read/s, 100 write/s.
- IDs: Series → Event → **Market ticker** (e.g. `KXPRESPARTY-28-D`).

### Polymarket (non-custodial, on-chain USDC/Polygon)
- Gamma discovery `https://gamma-api.polymarket.com` (`/markets`, `/events` →
  `condition_id`, `clobTokenIds`). CLOB `https://clob.polymarket.com` — public
  reads (`/book`, `/price`, `/midpoint`, `/spread`) need **no auth** (enough for v1).
- WS `wss://ws-subscriptions-clob.polymarket.com/ws/market`; channels `book`,
  `price_change`, `last_trade_price`; subscribe by `assets_ids` (token_id); PING ~5–10s.
- Trading auth (Phase 4): L1 EIP-712 → derive L2 API creds → HMAC-SHA256 per request;
  orders EIP-712 signed (domain "Polymarket CTF Exchange" v2, chainId 137).
- **Fee (UNVERIFIED):** historically ~0%; a 2026 schedule reportedly added small,
  category-dependent taker fees + maker rebates. Modeled as configurable bps,
  default 0 — **confirm on the official fee page before Phase 4**.
- SDK: `py-clob-client`. Gas is relayer-subsidized on Polygon.
- IDs: `condition_id` per market; `clobTokenIds` = YES/NO ERC-1155 `token_id`s;
  price(YES) + price(NO) ≈ $1.

### Facts to re-verify before relying on them
Polymarket exact 2026 fee schedule & current US-availability; Kalshi canonical order
endpoint (`/portfolio/orders` vs newer event-order path) & per-series fee multipliers;
both venues' official rate-limit numbers.

## 6. Setup

```bash
# 1. Create and activate a virtualenv (Windows PowerShell)
python -m venv .venv; .venv\Scripts\Activate.ps1

# 2. Install (v1 needs no trading SDKs)
pip install -e ".[dev]"

# 3. Configure secrets
copy .env.example .env      # then edit .env
copy config\config.example.yaml config\config.yaml   # optional tuning

# 4. Initialize the database
arb init-db

# 5. Verify config
arb check
```

### Step: Telegram bot setup (do once per environment)
1. In Telegram, open **@BotFather** → `/newbot` → follow prompts → copy the **bot token**.
2. Put it in `.env`: `ARB_TELEGRAM_BOT_TOKEN=<token>`.
3. Open your new bot in Telegram and **send it any message** (e.g. "hi").
4. Run `arb setup-telegram` — it discovers your **chat ID**, prints the
   `ARB_TELEGRAM_CHAT_ID=...` line to add to `.env`, and sends a confirmation.
5. Add that line to `.env`, then verify end-to-end with `arb test-alert`.

Secrets live only in `.env` (gitignored) — never commit them. To use another PC,
`git pull` the repo and recreate its own `.env` (the doc above is all you need).

## 6b. Analysis mode (Phase 1)

```bash
arb collect --limit 200      # sweep configured markets -> snapshot to SQLite
arb report                   # export reports/analysis.xlsx from snapshots
arb collect --venue kalshi   # optional: one venue only
```

Run `collect` on a schedule (cron / Task Scheduler); each run appends a snapshot,
so cross-run stats (price volatility, sample count) accrue over time. `report`
builds two sheets: **markets** (latest snapshot per market — YES/NO best bid/ask,
spread, ask-depth, volume, liquidity, volatility) and **summary** (per-venue
counts). Sorted by liquidity then volume.

**Discovery is venue-specific** (`config.yaml -> discovery`):
- **Polymarket** — its `/markets` listing is volume-sorted, so we just take the
  top N by volume. No curation needed.
- **Kalshi** — its `/markets` listing is >90% auto-generated sports parlays
  (`KXMVE...`, provisional), so blind top-N is useless. Instead we sweep curated
  **series** (each series = a recurring market family) and/or whole **categories**
  (resolved via `GET /series?category=`), dropping provisional/MVE noise. Default
  seeds are daily high-temp series (`KXHIGHNY/LAX/CHI`) — the "weather per day"
  case. Add econ/politics series or set `categories: ["Economics"]` to widen.

Note: cross-venue *matching* (deciding a Kalshi market and a Polymarket market are
the same outcome) is **Phase 2** — Phase 1 only gathers per-venue stats to inform
that shortlist.

## 7. Roadmap

- **Phase 0 — Scaffold** ✅ config, DB, logging, Telegram, anomaly, Provider ABC +
  models, fee models, CLI skeleton, tests.
- **Phase 1 — Analysis mode** ✅ Kalshi + Polymarket discovery/book fetch, `collector`
  cron entrypoint, snapshot storage, Excel `report`. *Deliverable: the Excel table.*
  See "Analysis mode" below.
- **Phase 2 — Matching & shortlisting:** `config/markets.yaml` canonical mapping +
  suggestion heuristics; rank candidate pairs by historical spread, fee-net edge,
  liquidity. *Deliverable: shortlist of viable pairs → seed markets chosen here.*
- **Phase 3 — Live engine (paper):** WS feeds w/ reconnect+resync, fee-net detection,
  book-walking sizer, paper simulator + P&L, anomaly → Telegram.
- **Phase 4 (gated, later) — Live execution:** implement `place_order` (Kalshi REST
  first, Polymarket EIP-712 second), explicit enable flag, semi-auto (Telegram
  confirm) before any full-auto.

## 8. Verification

```bash
pytest                 # unit (fees, models) + smoke (DB schema, config)
arb check              # resolved config & integration readiness
arb test-alert         # Telegram round-trip
```

Later phases add: `arb collect` → rows in SQLite; `arb report` → valid `.xlsx`;
`arb run` dry-run against real WS in paper mode with an injected anomaly confirming
a Telegram alert.
