Here's a practical, start-to-finish guide. Three config files matter: **`.env`** (secrets + the live-trading gate), **`config/config.yaml`** (tuning + execution mode), **`config/markets.yaml`** (the market pairs to trade). Wallet files like `.env.1pixel` you already have.

---

## 1. One-time setup

Install (add `live` for real trading later):
```bash
pip install -e ".[dev]"
```

Create your config files from the templates:
```bash
cp .env.example .env
```
```bash
cp config/config.example.yaml config/config.yaml
```
```bash
cp config/markets.example.yaml config/markets.yaml
```

**Edit `.env`** — for now just the basics (leave trading OFF):
```ini
ARB_ENVIRONMENT=demo
ARB_LIVE_TRADING=false
ARB_TELEGRAM_BOT_TOKEN=      # fill after BotFather (step below)
ARB_TELEGRAM_CHAT_ID=        # fill by running setup-telegram
```

Create the database:
```bash
arb init-db
```

Set up Telegram (in Telegram: message **@BotFather** → `/newbot` → copy token into `.env`, then message your new bot once):
```bash
arb setup-telegram
```
It prints your `ARB_TELEGRAM_CHAT_ID=...` — paste that into `.env`, then verify:
```bash
arb test-alert
```

Confirm everything (should say **"Trading gate: paper (safe)"**):
```bash
arb check
```

---

## 2. Find markets and pick pairs

Collect current market data into the DB:
```bash
arb collect --limit 200
```

Export the analysis table (open `reports/analysis.xlsx` to browse markets, prices, volume):
```bash
arb report
```

Get suggested cross-venue matches:
```bash
arb suggest --threshold 0.2
```

**Edit `config/markets.yaml`** — this is the important one. Each entry says "these two markets are the *same real-world outcome*." You get the ids from the report/suggest output:
- **Kalshi `market_id`** = the ticker, e.g. `KXHIGHNY-26AUG02-T84`
- **Polymarket `market_id`** = the `conditionId`, e.g. `0x1fad...772be`
- **`outcome`** = which side means *"the event happens"* on that venue (`yes` or `no`)

```yaml
pairs:
  - canonical_id: my-first-pair
    description: "Same event on both venues — describe it"
    legs:
      kalshi:
        market_id: KXHIGHNY-26AUG02-T84
        outcome: yes
      polymarket:
        market_id: "0x1fad72fae204143ff1c3035e99e7c0f65ea8d5cd9bd1070987bd1a3316f772be"
        outcome: yes
```
⚠️ Only add a pair after you've checked both venues' resolution rules actually match — a wrong pair invents fake profit.

Rank your pairs by fee-net edge:
```bash
arb shortlist
```

---

## 3. Run in paper mode (no money)

Watch it detect for 60 seconds:
```bash
arb run --mode paper --duration 60
```
Or run continuously (Ctrl+C to stop):
```bash
arb run --mode paper
```

Check simulated results:
```bash
arb pnl --out reports/pnl.xlsx
```

---

## 4. Go live (only when paper looks right)

This spends real money. Change **three things**, deliberately:

**In `.env`:**
```ini
ARB_ENVIRONMENT=prod
ARB_LIVE_TRADING=true
ARB_POLYMARKET_ENV_FILE=.env.1pixel
ARB_POLYMARKET_SIGNATURE_TYPE=2
```

**In `config/config.yaml`** set the mode (start with `semi_auto` so each trade needs your Telegram approval) and a small cap:
```yaml
execution:
  mode: semi_auto
  confirm_timeout_s: 120
  max_order_stake_usd: 20.0
```

Verify the gate now shows **LIVE**:
```bash
arb check
```

Run it (each detected arb sends a Telegram message; reply `yes <code>` to place):
```bash
arb run --mode semi_auto --wallet-env .env.1pixel
```

Once you trust it, and only then, switch `mode: auto` for hands-off placement.

---

## Command cheat-sheet

| When | Command |
|---|---|
| After config changes | `arb check` |
| First time / new machine | `arb init-db` |
| Set up alerts | `arb setup-telegram` → `arb test-alert` |
| Gather market data (schedule this) | `arb collect --limit 200` |
| Browse markets | `arb report` |
| Find pairs | `arb suggest` |
| Rank your pairs | `arb shortlist` |
| Test strategy (safe) | `arb run --mode paper` |
| See results | `arb pnl --out reports/pnl.xlsx` |
| Live w/ approval | `arb run --mode semi_auto --wallet-env .env.1pixel` |

**Golden rule:** real orders need **all** of `ARB_LIVE_TRADING=true` + `ARB_ENVIRONMENT=prod` + `mode: semi_auto|auto`. Miss any one and it stays paper — `arb check` always tells you which state you're in.

---

Want me to save this as **`docs/RUNBOOK.md`** in the repo so it's there on every machine? I can add it to the Phase 4b branch (or a separate small PR).