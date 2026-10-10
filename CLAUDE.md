# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

A **local trading executor** that consumes signals from the sibling `fib-accumulator` repo and places orders via Interactive Brokers (IBKR). It runs on the user's always-on Mac mini under `launchd`, ticks hourly, and is a broker-agnostic skeleton with IBKR as the only current implementation.

The repo was originally named `webull-bot` (renamed to `tradebot` on 2026-04-19 after settling on IBKR). Webull Australia has no stable public API, so Webull was never a real target — just a working title.

The signal contract is defined by `fib-accumulator/docs/executor-plan.md`. Any schema change there must be mirrored in `src/executor/signal.py` with a major-version bump.

## Commands

```bash
uv sync --all-extras --dev            # install deps + dev tools
uv run pytest                         # unit tests (schema, store, gates)
uv run ruff check                     # lint
uv run ruff check --fix               # auto-fix what ruff can

# Offline dry-run against the fixture — no GitHub, no IBKR needed
uv run python -m executor.execute --signal-file tests/fixtures/sample_signal.json

# Live tick — hits GitHub; will also hit IBKR if EXECUTE=true
uv run python -m executor.execute

# Smoke tests for external deps
uv run python scripts/smoke_poller.py   # GitHub PAT + fib-accumulator signals/
uv run python scripts/smoke_broker.py   # IB Gateway on localhost:4002
```

## Architecture

Data flow per tick (once per hour, ~HH:05):

```
launchd  ──►  execute.main()
                │
                ├─ halt_file gate  (HALT file at repo root aborts)
                ├─ market_is_open gate  (US equity RTH, skipped for --signal-file)
                ├─ SignalPoller.poll()
                │     └─ GitHub Contents API: GET /repos/<fib-repo>/contents/signals
                │        └─ For each new JSON, fetch + Pydantic validate
                │
                └─ For each Signal:
                      ├─ expired? → record "expired", skip
                      ├─ already in SQLite? → skip (idempotency)
                      ├─ HOLD? → record + receipt, skip
                      ├─ connect IBKRBroker (lazy, first tradeable signal)
                      ├─ per-signal gates: drift, buying_power|position, no_open_orders
                      ├─ EXECUTE=false? → record "dry-run" + receipt
                      └─ EXECUTE=true? → LimitOrder TIF=DAY, poll 5min, record fill/cancel
```

**Key design decisions** (locked with the user in the initial design session — don't re-litigate without checking in):

- **Fully local, no GitHub Actions.** The Mac mini is always-on, so GHA adds networking complexity for no benefit.
- **Submit-once limit orders.** Executor uses the producer's `limit_price` as-is. No intraday re-pricing. Hourly cron is just a polling loop for new signals, not an active price strategy.
- **No git push from the bot.** Receipts are written to disk; the user reviews and commits manually. Keeps git credentials out of the cron environment. The SQLite store is the authoritative record.
- **`EXECUTE=false` is the default.** User must explicitly enable live trading in `.env.local`. Plan: run against paper account for ≥2 weeks before flipping to live.
- **Fractional shares are rejected in Phase 1.** IBKR supports fractional via `cashQty` instead of `totalQuantity`, but the branching logic is Phase-2 work. For now, `qty_shares` must be a whole number or the signal is rejected with a clear message. Producer should round before emitting.
- **`signals/` 404 = no-op.** A 404 is silently treated as "no signals" — so a wrong `FIB_REPO` looks exactly like a quiet market. The repo is `Jarjarbinks8341/fib-accumulator` (private); verify with `scripts/smoke_poller.py` after any config change. (It sat on the wrong owner from 2026-04 to 2026-10 unnoticed.)
- **BUYs never use margin.** The producer sizes signals against its own `FIB_INITIAL_CAPITAL` ($50k), not this account. Every live BUY (executor or MCP) is gated on settled cash (`TotalCashValue`, not `BuyingPower` which includes ~6.7x margin), plus `MAX_ORDER_USD` per order and `MAX_DAILY_USD` rolling-24h across both sources (`placed_orders` table).
- **MCP server places real orders** (`tradebot-mcp`, stdio only). Two-step `preview_order` → `place_order(preview_id)`; preview is single-use, expires in 5 min; HALT and daily cap re-checked at place time. Limit orders, whole shares, fat-finger guard (`MAX_LIMIT_FROM_QUOTE_PCT`). Uses `IB_MCP_CLIENT_ID` (18) so it never collides with the executor (17).
- **Options via MCP only: cash-secured puts + long calls.** `preview_option_order` opens only SELL put (assignment cash `strike×100` must be covered by settled cash minus collateral of existing short puts and working put sells) or BUY call (≥ `MIN_LONG_CALL_DTE`, default 180). BUY put / SELL call are allowed only to close a held position — no naked calls, no long puts. Put collateral counts toward `MAX_PUT_COLLATERAL_USD` and `MAX_DAILY_USD`; call premium toward `MAX_ORDER_USD` and `MAX_DAILY_USD`. Stock BUYs (executor + MCP) use `get_free_cash()` so they can't spend cash securing puts. The hourly executor never trades options.

## Module map

| Path | Purpose |
|---|---|
| `src/executor/signal.py` | Pydantic `Signal` + `Action` (StrEnum). Rejects unknown schema majors via `field_validator`. |
| `src/executor/poller.py` | GitHub Contents API client. 404 on `signals/` → empty list, no error. |
| `src/executor/store.py` | SQLite idempotency store. `already_executed` + upsert `record`. |
| `src/executor/gates.py` | Pure `(ok, reason)` functions: halt file, market hours (hard-coded NYSE holidays 2026-2027), drift, buying power, positions, open orders. |
| `src/executor/broker/base.py` | `BrokerClient`/`OptionsBroker` `Protocol`s + `OrderHandle`/`OrderResult`/`OptionContract`/`OptionQuote` dataclasses. |
| `src/executor/broker/ibkr.py` | `ib-async` implementation. Caches buying power + positions per-invocation to respect rate limits. |
| `src/executor/order_desk.py` | Preview → place state machine + gates for manual (MCP) orders. Pure enough to test with a fake broker. |
| `src/executor/mcp_server.py` | FastMCP tools: `get_account`, `get_quote`, `preview_order`, `get_option_chain`, `get_option_quote`, `preview_option_order`, `place_order`, `list_open_orders`, `cancel_order`. |
| `src/executor/execute.py` | Orchestration entrypoint. `--signal-file` for offline fixture dry-runs. |
| `src/executor/config.py` | `.env.local` / `.env` loader; frozen `Settings` dataclass. |
| `tests/fixtures/sample_signal.json` | Canonical schema example. Dates in 2099 so `is_expired()` is always false. |

## Mac mini setup runbook

Run once, on the Mac mini.

### 1. Clone + install deps

```bash
cd /Users/jiazhongchen/repo
git clone <remote> tradebot   # or already cloned
cd tradebot
uv sync --all-extras --dev
cp .env.example .env.local
# Edit .env.local: FIB_REPO, GITHUB_TOKEN (fine-grained PAT, read-only on fib-accumulator)
```

### 2. Install IB Gateway + IBC

IBKR doesn't offer a headless install — download interactively.

1. Download **IB Gateway — Stable, Apple Silicon** from https://www.interactivebrokers.com/en/trading/ibgateway-stable.php.
   - The installer defaults to `/Users/<you>/Applications/IB Gateway <version>/` — **accept that path**. IBC (macOS) expects exactly this layout (`${tws_path}/IB Gateway ${tws_version}/jars/`, see `scripts/ibcstart.sh:246`).
   - Launch the app once from Finder and close, so Gatekeeper accepts it.
   - Note the version (e.g. `10.37`).
2. Download **IBC (IBCMacos-3.x.x.zip)** from https://github.com/IbcAlpha/IBC/releases. Unpack to `/Users/<you>/ibc`:
   ```bash
   mkdir -p ~/ibc && cd ~/ibc && unzip ~/Downloads/IBCMacos-*.zip
   chmod +x *.sh scripts/*.sh
   ```
3. Edit `~/ibc/config.ini` (already created by the zip). Set:
   - `IbLoginId=<paper-username>`
   - `IbPassword=<paper-password>`
   - `TradingMode=paper`
   - `IbDir=/Users/<you>/Applications/IBJts` (placeholder — IBC macOS ignores this, but keep non-empty)
   - `BypassOrderPrecautions=yes`
   - `ReadOnlyApi=no`
   Then `chmod 600 ~/ibc/config.ini`.
4. Edit `~/ibc/gatewaystartmacos.sh` — change the hardcoded defaults at the top (the script ignores env vars and CLI args for these):
   - `TWS_MAJOR_VRSN=10.37` (match installed version)
   - `TRADING_MODE=paper`
   - `IBC_PATH=~/ibc`
   - `TWS_PATH=~/Applications`
5. Create the settings dir IBC checks for: `mkdir -p ~/Jts`.
6. Smoke: run `~/ibc/gatewaystartmacos.sh` manually. On first launch, an SSL-encryption dialog appears — click **Reconnect using SSL**. Then in another terminal: `lsof -iTCP:4002 -sTCP:LISTEN` should show the Java process.

### 3. Verify bot connects

```bash
uv run python scripts/smoke_poller.py   # expect: signals listed (or "producer not live yet")
uv run python scripts/smoke_broker.py   # expect: buying power printed
```

### 4. Install launchd jobs

```bash
# Fill in the IB Gateway version placeholder first:
vim scripts/launchd/com.jiazhongchen.ibgateway.plist
#   replace __FILL_IN__IB_GATEWAY_VERSION__ with e.g. 1037

cp scripts/launchd/com.jiazhongchen.*.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.jiazhongchen.ibgateway.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.jiazhongchen.tradebot.plist

launchctl list | grep -E 'ibgateway|tradebot'
launchctl kickstart -k gui/$(id -u)/com.jiazhongchen.tradebot   # run once now to verify
tail -f ~/Library/Logs/tradebot.out.log
```

### 5. Paper first

Leave `EXECUTE=false` until at least 2 weeks of clean ticks against the paper account, with real signals coming from fib-accumulator. Only then flip the `.env.local` values:

```
EXECUTE=true
IB_PORT=4001          # live — was 4002 for paper
```

And update `IbLoginId`/`TradingMode` in IBC config.

## Operational gotchas

- **Holiday list is hard-coded** in `gates.py` through 2027. Refresh annually from https://www.nyse.com/markets/hours-calendars.
- **IB Gateway auto-logout** at ~01:00 ET daily. IBC handles re-login; the bot ignores the ~1-minute connection-refused window and retries next hour.
- **`Minute=5` cron** is deliberate — dodges top-of-hour traffic and the IB Gateway restart window.
- **No options market data over the API** without an OPRA subscription: option quotes come back with last/close only (no bid/ask/Greeks), so the option fat-finger guard falls back to last, then close.
- **Rate limits**: the IBKR broker caches `buying_power` and `positions` for the lifetime of one invocation. Don't add call sites that bypass the cache.
- **No log rotation**: launchd doesn't rotate `~/Library/Logs/tradebot.*.log`. Clean up manually or add a `newsyslog.d` config later.
- **Clock drift** would make `is_expired` lie. macOS syncs NTP by default; verify with `sudo systemsetup -getnetworktimeserver`. The executor logs `now_utc` at the top of every tick for visibility.
- **HALT file** at the repo root aborts the bot immediately. Use `touch HALT` to stop all trading without unloading launchd.

## Known limitations (Phase 1)

Deferred intentionally — revisit when paper trading is stable:

- Fractional shares (needs `cashQty` path in IBKR broker).
- Alerts on failure (Telegram/Slack webhook on auth fail, gate reject, order reject).
- Daily reconciliation workflow against IBKR fills.
- Monthly P&L reporting.
- Intraday re-pricing.
