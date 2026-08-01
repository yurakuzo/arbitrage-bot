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
    cli.py                # `arb`: check/init-db/setup-telegram/collect/report/suggest/shortlist/run/pnl
    providers/
      base.py             # Provider ABC — the pluggable interface
      models.py           # normalized market/book types + Order/OrderResult
      kalshi.py           # discovery + books; gated RSA-signed place_order  [done]
      polymarket.py       # Gamma discovery + CLOB books/WS; gated place_order [done]
      auth/kalshi_auth.py     # Kalshi RSA-PSS request signer                 [done]
      auth/polymarket_auth.py # wallet-env credential loader (redacts key)    [done]
    core/
      fees.py             # per-venue fee models (Kalshi convex; Polymarket bps)  [done]
      pricing.py          # fee-net edge + book-walking sizing (arb detection)  [done]
      matching.py         # curated mapping loader + suggestion heuristics       [done]
    analysis/
      collector.py        # cron entrypoint: sweep -> snapshot -> DB         [done]
      report.py           # DB -> Excel per-market stats                     [done]
      shortlist.py        # rank curated pairs by fee-net edge -> Excel      [done]
    live/
      engine.py           # feeds -> detect -> execute                       [done]
      feeds.py            # Polymarket WS + Kalshi polling feeds             [done]
      gate.py             # TradingGate — multi-flag real-order guard        [done]
      executor.py         # paper/semi_auto/auto routing + outcome mapping   [done]
      simulator.py        # paper fills + P&L ledger                         [done]
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
- `place_order(order, gate)` — real order, **gate-guarded** (Phase 4). Kalshi
  implemented; Polymarket stubbed (4b). Default raises so a venue can't trade
  by accident.

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

## 6c. Matching & shortlisting (Phase 2)

```bash
arb suggest --threshold 0.25      # propose candidate cross-venue matches (review only)
# ...review, then hand-add confirmed pairs to config/markets.yaml...
arb shortlist                     # rank curated pairs by fee-net edge -> reports/shortlist.xlsx
```

**Matching is curated, never automatic.** `arb suggest` compares collected market
titles (token Jaccard similarity) and prints ranked *candidates* — it never
confirms them. You verify each against the real resolution rules on both venues,
then add confirmed entries to `config/markets.yaml` (schema in
`config/markets.example.yaml`), aligning each venue's "event happens" side. A
wrong mapping manufactures fake edge and real losses.

`arb shortlist` then, for each mapping, pulls the latest snapshot per leg and
computes the **fee-net** arbitrage edge in both directions
(`core.pricing.top_of_book_edge`), ranking by net edge per contract and tradable
depth. Size/profit figures are an optimistic top-of-book proxy; exact book-walked
sizing is live in Phase 3 (`core.pricing.find_arbitrage`).

> Reality check: Kalshi's liquid recurring markets (weather, sports) and
> Polymarket's (politics, culture) **overlap thinly**. Expect a small, curated set
> of genuinely matchable pairs — quality over quantity.

`config/markets.yaml` is *not* gitignored — commit your curated mappings to sync
them across machines (like this doc).

## 6d. Live paper engine (Phase 3)

```bash
arb run                       # run until Ctrl+C over config/markets.yaml pairs
arb run --duration 60         # bounded run (e.g. for cron / testing)
arb run --poll                # force REST polling for all venues (no WebSocket)
arb pnl                       # summarize the paper-trade ledger (net P&L, by pair/venue)
```

`arb pnl` recomputes P&L directly from the `paper_fills` rows (payout − cost −
fees per matched pair), so the summary is self-verifying rather than trusting a
stored number. It breaks results down by canonical pair and by venue (including
fees paid), and lists the most recent trades.

For every curated pair, the engine maintains live order books and computes the
fee-net arbitrage in real time (`core.pricing.find_arbitrage`, walking both
books). A profitable, **fresh** opportunity above `thresholds.min_edge` is sized
against `thresholds.max_stake_usd` and recorded by the **paper simulator**
(`paper_fills` table + running P&L). No real orders are ever placed.

Feeds:
- **Polymarket** — public **WebSocket** (`book` snapshot + `price_change` deltas),
  auto-reconnect with backoff, periodic re-emit so a healthy socket keeps books
  fresh.
- **Kalshi** — **REST polling** (~3s). Its WebSocket requires RSA auth, so live
  Kalshi streaming is deferred to Phase 4; `--poll` also forces this everywhere.

Safety/robustness:
- **Staleness guard** — a quote older than `max_quote_age_ms` is never traded on;
  it raises a `stale_quote` anomaly → Telegram. Keep the threshold above the feed
  cadence (default 8s).
- **Anomaly routing** — feed errors, reconnects, and staleness all flow through
  `infra.anomaly` and escalate to Telegram (warnings/criticals).
- **Hysteresis** — each opportunity records once; it must close (edge ≤ 0) before
  it can re-trigger, so a persistent gap doesn't spam fills/alerts.

## 6e. Live execution (Phase 4 — gated, off by default)

> **v1 default is paper. No real order can leave the process unless you
> deliberately open every gate below.** Building this is not a recommendation to
> trade; real-money operation, and the regulatory question (Polymarket blocks US
> persons; Kalshi is US-only), are the operator's responsibility.

### The trading gate (`live.gate.TradingGate`)
A real order is placed **only if ALL** hold (else it transparently falls back to
paper):
1. `ARB_LIVE_TRADING=true` (env master switch)
2. `execution.mode` is `semi_auto` or `auto` (config)
3. `ARB_ENVIRONMENT=prod`
4. per-order stake ≤ `execution.max_order_stake_usd`

Every `provider.place_order` calls `gate.check_order()` first — there is no code
path to a real order that bypasses it. Check your status any time:
```bash
arb check      # -> "Trading gate: paper (safe)"  or  "LIVE (places real orders)"
```

### Execution modes (`arb run --mode ...` or `config.execution.mode`)
- **paper** — simulate + record P&L (default; always safe).
- **semi_auto** — on each hit, send a Telegram proposal; place **only** on an
  explicit `yes <nonce>` reply within `confirm_timeout_s` (timeout ⇒ skip).
- **auto** — place both legs immediately (only when the gate is fully open).

Outcome translation: the engine detects in a canonical (YES == event) space; the
executor maps each leg back to the venue's **native** yes/no side before ordering
(`live.executor.native_outcome`) — verified by tests, since a flip would place the
opposite bet.

### Venue status
- **Kalshi** ✅ `place_order` implemented: RSA-PSS request signing
  (`providers.auth.kalshi_auth`), `POST /portfolio/orders`, prices in cents. Needs
  `ARB_KALSHI_API_KEY_ID` + `ARB_KALSHI_PRIVATE_KEY_PATH` and `pip install -e '.[live]'`.
- **Polymarket** ✅ `place_order` implemented (Phase 4b) via `py-clob-client`:
  loads a **wallet dotenv file** (`ARB_POLYMARKET_ENV_FILE` or `arb run
  --wallet-env .env.1pixel`) containing `PRIVATE_KEY` + `PROXY_WALLET`, derives L2
  API creds (L1 EIP-712), resolves the outcome→CLOB token, and posts a FOK order.
  Prereqs: `pip install -e '.[live]'`, a funded Polygon wallet with one-time
  USDC/CTF approvals, and `ARB_POLYMARKET_SIGNATURE_TYPE` matching the wallet
  (1 = email/magic, 2 = browser/Gnosis Safe). Multiple wallets = multiple env
  files; pick one per run. Secrets are read at call time (never into global env)
  and redacted in logs.

Leg risk: legs are placed sequentially; if the second fails after the first fills,
a `CRITICAL` anomaly fires (→ Telegram) flagging manual review. True atomicity
isn't possible across two independent venues — size conservatively.

## 7. Roadmap

- **Phase 0 — Scaffold** ✅ config, DB, logging, Telegram, anomaly, Provider ABC +
  models, fee models, CLI skeleton, tests.
- **Phase 1 — Analysis mode** ✅ Kalshi + Polymarket discovery/book fetch, `collector`
  cron entrypoint, snapshot storage, Excel `report`. *Deliverable: the Excel table.*
  See "Analysis mode" below.
- **Phase 2 — Matching & shortlisting** ✅ `config/markets.yaml` canonical mapping +
  title-similarity suggestions; fee-net arbitrage pricing (`core.pricing`); ranked
  shortlist. *Deliverable: shortlist of viable pairs → seed markets chosen here.*
  See "Matching & shortlisting" below.
- **Phase 3 — Live engine (paper)** ✅ Polymarket WS + Kalshi polling feeds, real-time
  fee-net detection over curated pairs, book-walking sizer, paper simulator + P&L,
  anomaly → Telegram. See "Live paper engine" below.
- **Phase 4 — Live execution (gated)** ✅ *code complete, off by default.*
  `TradingGate` (multi-flag, default paper), execution modes (paper/semi_auto/auto),
  Telegram-confirm, Kalshi RSA-signed `place_order`, **Polymarket EIP-712 placement
  (4b)** via `py-clob-client` + wallet-env selection. Real-money operation (and the
  regulatory question) is the user's decision; needs a funded wallet + approvals.
  See "Live execution" below.

## 8. Verification

```bash
pytest                 # unit (fees, models) + smoke (DB schema, config)
arb check              # resolved config & integration readiness
arb test-alert         # Telegram round-trip
```

Later phases add: `arb collect` → rows in SQLite; `arb report` → valid `.xlsx`;
`arb run` dry-run against real WS in paper mode with an injected anomaly confirming
a Telegram alert.

### CI
GitHub Actions (`.github/workflows/ci.yml`) runs `ruff check` + `pytest` on every
push/PR to `main`, across Python 3.11 and 3.13. The suite is fully offline (no API
calls), so CI is deterministic and needs no secrets.
