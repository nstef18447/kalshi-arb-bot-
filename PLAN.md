# Kalshi Multi-Series Arb Bot + Market Maker — Project Plan

## Current Status (2026-02-26)

### What's Built & Running
The system runs **two modes** on a DigitalOcean droplet (`159.65.44.106`):

1. **Arb Scanner** (MODE=read_only or MODE=arb) — scans 8 series for cross-strike arbitrage
2. **Market Maker** (MODE=market_maker) — quotes bid/ask on ATM KXBTCD strikes, captures spread P&L

Currently running **LIVE** (`MM_CONFIRM=true`, `MM_QUOTE_SIZE=2`) since 2026-02-26. Multi-expiry tiered quoting active (hourly preferred > daily fallback > weekly exit-only). Phase 2.1 improvements: volatility-adaptive spread, smart requoting, volatility pause. Position reconciliation and fill-safe strike rotation deployed.

### Architecture
```
Bot (systemd: kalshi-bot)         Dashboard (not yet deployed)
  MODE=arb:                           dashboard.py (Streamlit)
    bot.py -> scanner.py              queries.py (read)
  MODE=market_maker:                     |
    mm_engine.py -> mm_config.py         |
    mm_logger.py (write)                 |
         \                             /
          -----> arb_bot.db <---------
                (WAL mode)
```

### File Inventory

| File | Purpose | Status |
|------|---------|--------|
| `auth.py` | RSA-PSS signing, authenticated requests | Done |
| `config.py` | Mode selector, per-series fees, READ_ONLY flag | Done |
| `kalshi_api.py` | API wrappers (markets, orderbook, orders, positions, open_orders) | Done |
| `scanner.py` | Ladder builder, stale filter, opportunity detector (Type A/B/C) | Done |
| `bot.py` | Arb scan loop, multi-series, maker profit, execution logic | Done |
| `mm_config.py` | MM configuration from env vars (spread, vol, inventory, loss, tier cutoffs) | Done |
| `mm_engine.py` | MarketMaker class — multi-expiry tiers, dynamic spread, smart requoting, vol pause, FIFO P&L, position reconciliation, exit-only strikes | Done |
| `mm_logger.py` | DB logging for MM (quotes, fills, snapshots) | Done |
| `main.py` | Entry point, branches on MODE for arb vs market_maker | Done |
| `db.py` | SQLite schema (7 tables), migrations, connection helpers | Done |
| `db_logger.py` | Arb scanner write helpers + maker summary query | Done |
| `queries.py` | SQL queries for dashboard (returns DataFrames) | Done |
| `monitor.py` | CLI monitoring script (DB + API, --compact mode) | Done |
| `sports_scan.py` | Sports market feasibility scanner | Done |
| `sports_feasibility.md` | Sports MM feasibility report (verdict: not viable) | Done |
| `dashboard.py` | 5-page Streamlit app | Done (not deployed) |
| `deploy.sh` | Deployment script (systemd setup) | Done |

### Deployment Details
- **Droplet**: `ssh root@159.65.44.106`
- **Bot path**: `/opt/kalshi-arb-bot`
- **Repo**: `https://github.com/nstef18447/kalshi-arb-bot-.git`
- **Service**: `systemd: kalshi-bot` (auto-restart, PYTHONUNBUFFERED=1)
- **Env**: `.env` with `KALSHI_ENV=prod`, `MODE=market_maker`, `MM_CONFIRM=true`
- **API**: `https://api.elections.kalshi.com/trade-api/v2` (production)
- **Account**: ~$47.86 balance (started $50, ~$2.14 in stranded inventory + fees)
- **Monitor**: `cd /opt/kalshi-arb-bot && ./venv/bin/python3 monitor.py [--compact]`

---

## Market Maker Engine

### Motivation
MM simulation on KXBTCD showed $25 spread P&L over 5.3 days (97% from spread capture, not directional). The engine runs as an alternative mode alongside the arb scanner.

### How It Works

**Multi-expiry tiered strike selection (every 5 min):**
- Fetch all KXBTCD markets, classify events by TTL:
  - **Hourly** (TTL < 2h): always quote — prefer shortest TTL
  - **Daily** (TTL 2h–26h): quote only if fewer than 2 hourly strikes active
  - **Weekly** (TTL > 26h): never open new positions, exit-only for existing inventory
- Within each tier: probe ~10 middle strikes, find ATM (best yes bid nearest 50c)
- Select ATM + 1 above + 1 below = 3 strikes per event
- On startup: print available events table with tier, TTL, spread, and quotable status
- `_pick_atm_from_events()`: extracted helper for per-tier ATM finding

**Exit-only strike management:**
- Strikes with inventory that leave the active ATM set get `exit_only=True`
- Exit-only strikes are kept stable — no churn from repeated fill-check/cancel/re-adopt
- Always post the reducing side (ask if inv>0, bid if inv<0) regardless of book conditions
- `_compute_exit_price()` falls back to partial book data or `last_mid` for exit pricing
- Once inventory reaches zero, strike is cleanly removed

**Position reconciliation (startup):**
- Query Kalshi `get_positions()` API
- For tracked tickers: update inventory if mismatched
- For untracked tickers: create StrikeState and adopt (exit-only)
- Runs after stale order cancellation

**Fill-safe strike rotation:**
- `_cancel_if_active` checks fills BEFORE cancelling and again if cancel fails
- `_handle_removed_strikes` does final `_check_fills` on every newly removed strike
- Prevents stranded positions from fills between last check and strike removal

**Volatility tracking (every 5s cycle):**
- Sample contract midprice from ATM strike's orderbook (not BTC spot)
- Record `(timestamp, midprice)` in rolling deque (max 60 entries)
- Record `(timestamp, atm_strike_value)` for BTC % move detection
- Calculate stdev of mid[t] - mid[t-1] changes over last 60 scans
- EMA smoothing: `ema_vol = alpha * raw_stdev + (1 - alpha) * ema_vol` (alpha=0.3)

**Dynamic spread (Phase 2.1):**
- `dynamic_half_spread = max(base, math.ceil(ema_vol * multiplier))`
- Capped at MM_MAX_HALF_SPREAD (15c = 30c total)
- Requires >= 10 observations before computing (uses base until then)
- Conservative rounding via math.ceil (wider is safer)

**Quote computation (every 5s per strike):**
- `mid = (best_yes_bid + (100 - best_no_bid)) / 2`
- Inventory skew: `adjusted_mid = mid - (inventory * 1c)`
- `target_bid = int(adjusted_mid - dynamic_half_spread)`
- `target_ask = int(adjusted_mid + dynamic_half_spread)`
- Safety clamps: don't cross book, 1 ≤ bid < ask ≤ 99
- Skip if native spread < MM_MIN_BOOK_SPREAD (3c) — except for exit-only reducing side

**Order mechanics (Kalshi-specific):**
- **Bid** (buy Yes): `create_order(ticker, "yes", target_bid, size, post_only=True)`
- **Ask** (sell Yes = buy No): `create_order(ticker, "no", 100 - target_ask, size, post_only=True)`
- `post_only=True` ensures orders rest as maker (rejected if they'd cross spread)

**Smart requoting (Phase 2.1 — queue priority preservation):**
- KEEP existing order if ALL of:
  - (a) Price within MM_QUOTE_TOLERANCE (2c) of target
  - (b) Correct side of spread (bid < best ask, ask > best bid)
  - (c) Not stale (resting < MM_STALE_QUOTE_SECONDS = 300s)
  - (d) Still competitive (not behind best bid/ask for > 30s)
- CANCEL AND REPLACE if any condition fails
- Stale timer resets on partial fills
- Tracks: requotes_skipped, requotes_needed, avg_queue_time

**Fill detection (delta-based, with safety checks):**
- Each cycle: `get_order(order_id)` for resting orders
- Track `last_remaining` per order, only record `delta = last_remaining - remaining`
- `_cancel_if_active` checks fills before cancelling, and again if cancel fails
- When order fully filled/cancelled: clear order_id, reset tracking

**FIFO P&L matching:**
- yes_fills and no_fills deques per strike
- Match oldest yes against oldest no: `profit = (100 - yes_price - no_price) * qty - fees`
- Fees use maker_mult from config.SERIES (not hardcoded)

**Volatility pause (Phase 2.1 — dual trigger):**
- **Primary trigger**: contract midprice moves > 15c in 60s
- **Backstop trigger**: ATM strike shifts > 0.3% of BTC price
- On trigger: cancel all quotes, log "VOL PAUSE"
- During pause: keep collecting midprice/ATM data (don't place orders)
- Resume requires:
  - Full lookback period (60s) elapsed since pause start
  - Fresh post-pause data (>= 12 samples) shows calm
  - Both triggers must be below thresholds using ONLY post-pause data

**Circuit breakers:**
- MAX_LOSS: total_realized_pnl < -MM_MAX_LOSS → halt, cancel all
- MAX_INVENTORY: abs(inventory) >= limit → stop quoting the increasing side
- API_ERRORS: consecutive_errors >= 5 → halt
- NARROW_SPREAD: native spread < 3c → skip quoting (except exit-only)
- VOL_PAUSE: large price move → pull all quotes until calm

**Startup:**
- Cancel all stale open orders from previous session
- Reconcile positions from Kalshi API (adopt stranded inventory)
- Print available events table by tier
- Log starting balance

**Shutdown (SIGTERM/SIGINT):**
- `_stopped` guard prevents double-call
- Cancel all resting orders (with fill check before each cancel)
- Log final inventory + P&L per strike

### MM Configuration
```
MODE=market_maker
MM_SERIES=KXBTCD
MM_HALF_SPREAD=5            # legacy (used as default for BASE_HALF_SPREAD)
MM_QUOTE_SIZE=2              # 2 contracts per quote (live test size)
MM_MAX_INVENTORY=5           # Max 5 contracts net position per strike
MM_MAX_LOSS=2500             # $25 max loss
MM_STRIKES=auto              # Auto-select ATM strikes
MM_REQUOTE_INTERVAL=5        # Re-evaluate quotes every 5s
MM_CONFIRM=true              # LIVE — real orders on Kalshi
MM_QUOTE_TOLERANCE=2         # Don't requote if within 2c of target
MM_MIN_BOOK_SPREAD=3         # Skip if native spread < 3c
MM_MAX_API_ERRORS=5          # Halt after 5 consecutive errors

# Multi-expiry tier system
MM_MIN_EXPIRY=600            # 10 min minimum TTL
MM_HOURLY_MAX_TTL=7200       # <2h = hourly tier (preferred)
MM_DAILY_MAX_TTL=93600       # <26h = daily tier (fallback)
MM_QUOTE_WEEKLY=false        # Don't open new positions on weekly events

# Phase 2.1 — Volatility-adaptive spread
MM_BASE_HALF_SPREAD=5        # Minimum half spread (cents)
MM_VOL_MULTIPLIER=2.0        # How aggressively to widen on vol
MM_MAX_HALF_SPREAD=15        # Cap (15c half = 30c total)
MM_VOL_WINDOW=60             # Rolling window: 60 scans = 5 min
MM_VOL_EMA_ALPHA=0.3         # EMA smoothing factor

# Phase 2.1 — Volatility pause (dual trigger)
MM_VOL_PAUSE_THRESHOLD=0.003 # 0.3% ATM strike move triggers pause
MM_MID_MOVE_PAUSE=15         # 15c contract mid move triggers pause
MM_VOL_PAUSE_LOOKBACK=12     # 12 scans = 60s lookback

# Phase 2.1 — Smart requoting
MM_STALE_QUOTE_SECONDS=300   # Cancel after 5min with no fills
MM_COMPETITIVENESS_CHECK_AGE=30  # Requote if behind best for 30s
```

### MM DB Tables
```sql
-- mm_quotes: every quote placement/cancel
(id, timestamp, ticker, side, price, size, action)

-- mm_fills: detected fills with running P&L
(id, timestamp, ticker, side, price, count, inventory_after, realized_pnl_cumulative)

-- mm_snapshots: per-cycle state per strike
(id, timestamp, cycle, ticker, strike, bid_price, ask_price,
 inventory, strike_realized_pnl, total_realized_pnl)
```

### Bugs Fixed (2026-02-26)
1. **Fill double-counting** — was re-reading total filled count each cycle; now tracks delta via `last_remaining`
2. **Missing post_only** — orders could cross spread as taker (4x fees); now `post_only=True` on all MM orders
3. **stop() double-call** — `_stopped` guard added
4. **Hardcoded fees** — now pulled from `config.SERIES[MM_SERIES]`
5. **No stale order cleanup** — startup now queries and cancels all open orders
6. **ATM probe wrong** — was reading lowest yes level instead of best bid; fixed to `max()`
7. **MAX_LOSS too high** — reduced from $50 to $25 to match balance
8. **CRITICAL: Stranded fills on strike rotation** — fills between last check and strike removal went undetected. Fix: fill checks in `_cancel_if_active`, final fill checks in `_handle_removed_strikes`, position reconciliation on startup.
9. **Exit-side not quoting** — when book empty or narrow spread, bot stopped quoting the exit side for inventory strikes. Fix: `_compute_exit_price` with `last_mid` fallback, always post reducing side.
10. **Strike churn on exit-only** — constant "Final fill check / Keeping despite ATM shift" every 5 min interrupted quoting. Fix: `exit_only` flag makes strikes stable until inventory clears.

### Phase 2.1 Improvements (2026-02-26)
**Volatility-adaptive spread:**
- Dynamic spread widens in volatile markets, floors at base
- EMA smoothing (alpha=0.3) reduces spread jitter
- math.ceil() for conservative rounding (wider is safer)
- Requires >= 10 observations before computing stdev
- Samples ATM strike specifically for consistent vol regime

**Smart requoting (queue priority):**
- Preserves queue priority — only cancel/replace when needed
- Competitiveness check: requote if behind best bid/ask for > 30s
- Stale timer resets on partial fills
- Spread floor prevents penny wars

**Volatility pause:**
- Dual trigger: 15c contract mid move OR 0.3% ATM strike shift
- Fresh-data resume: waits full lookback period, uses only post-pause data
- Prevents adverse selection during sharp BTC moves

### Multi-Expiry Tier System (2026-02-26)
- Replaced single `MM_MAX_EXPIRY` with 3-tier priority: hourly > daily > weekly
- Hourly events (TTL < 2h): always preferred, shortest TTL first
- Daily events (TTL 2h-26h): fallback when < 2 hourly strikes available
- Weekly events (TTL > 26h): exit-only, never open new positions
- Startup prints available events table with tier, TTL, spread, quotable status

---

## Sports Market Feasibility (2026-02-26) — REJECTED

Full report: `sports_feasibility.md`

**Key findings:**
- 4,636 active events, 2,288 categorized as Sports
- Top volume: Super Bowl futures (KXSB) — 882K contracts but **1c spreads**
- Out of 6,000 sports markets scanned: **1** had both a bid and ask
- 0 markets with spread >= 3c and meaningful volume
- Game-day markets (NBA props, totals): **zero volume, empty orderbooks**
- No above/below ladder structure — single yes/no outcomes only

**Verdict: NOT VIABLE for market making.** Professional MMs have compressed sports spreads to 1-2c. No game-day liquidity. Wrong market structure (no strike ladders). Stick with crypto above/below.

---

## Arb Scanner (Original Mode)

### Series Being Scanned

#### Above/below contracts — arb-eligible
| Series | Underlying | Type | ~Markets | Durations | Schedule |
|--------|-----------|------|----------|-----------|----------|
| **KXBTCD** | Bitcoin | Above/below | 165 | 1h / 25h / 169h | 24/7 |
| **KXETHD** | Ethereum | Above/below | 165 | 1h / 25h / 169h | 24/7 |
| **KXSOLD** | Solana | Above/below | 200 | 1h / 25h / 169h | 24/7 |
| **KXINXU** | S&P 500 | Above/below | 60 | 25h | US market hours |
| **KXNASDAQ100U** | Nasdaq-100 | Above/below | 60 | 25h | US market hours |

#### Range contracts — monitoring only
| Series | Underlying | Type | ~Markets |
|--------|-----------|------|----------|
| KXBTC | Bitcoin | Range | 165 |
| KXETH | Ethereum | Range | 165 |
| KXSOLE | Solana | Range | 200 |

### Key Decisions Made
- **Above/below vs Range**: Cross-strike arb logic only works on above/below contracts. Range contracts have independent buckets.
- **KXBTCD/KXETHD/KXSOLD**: The "D" suffix = directional (above/below). Primary arb targets.
- **Events->Markets two-step**: Must fetch events first, then markets per event.
- **Defense-in-depth READ_ONLY**: Guards at bot logic AND API layer.
- **Production URL**: `api.elections.kalshi.com` (not `trading-api.kalshi.com`)
- **Stale quote filter**: Depth < 10 or combined > 110 = phantom quotes
- **close_time vs expiration_time**: Use `close_time` for TTL, not settlement time
- **Per-series taker fees**: Parabolic `mult * P * (1-P)` per leg

---

## Execution Logic (Arb Mode)

### Pre-flight liquidity check
- Both sides must have >= MIN_DEPTH (30) contracts at best ask

### Leg the illiquid side first
- Place thinner side first, wait FIRST_LEG_TIMEOUT (2s)
- If fills → place second leg immediately
- If not → cancel, zero exposure

### Orphan fill recovery
- If second leg fails within SECOND_LEG_TIMEOUT (3s):
  - Cancel unfilled second leg
  - Sell filled first leg at best bid
  - Log realized P&L

### Circuit breaker
- Rolling window of 20 attempts
- Orphan rate > 25% → pause 10 min

---

## Config Variables (Arb Mode)
```python
MODE = os.getenv("MODE", "read_only").lower()
READ_ONLY = MODE not in ("arb", "market_maker")

ARB_THRESHOLD = 95            # Max combined price in cents
MAX_CONTRACTS = 25            # Contracts per leg
POLL_INTERVAL = 5             # Seconds between scan cycles
MAX_EXPOSURE = 50000_00       # $50k max capital

SERIES = {
    "KXBTCD":       {"taker_fee": 0.07,  "maker_mult": 0.0175, "poll_every": 1, "contract_type": "above_below"},
    "KXETHD":       {"taker_fee": 0.07,  "maker_mult": 0.0175, "poll_every": 1, "contract_type": "above_below"},
    "KXSOLD":       {"taker_fee": 0.07,  "maker_mult": 0.0175, "poll_every": 1, "contract_type": "above_below"},
    "KXINXU":       {"taker_fee": 0.035, "maker_mult": 0.00875, "poll_every": 6, "contract_type": "above_below"},
    "KXNASDAQ100U": {"taker_fee": 0.035, "maker_mult": 0.00875, "poll_every": 6, "contract_type": "above_below"},
    "KXBTC":        {"taker_fee": 0.07,  "maker_mult": 0.0175, "poll_every": 6, "contract_type": "range"},
    "KXETH":        {"taker_fee": 0.07,  "maker_mult": 0.0175, "poll_every": 6, "contract_type": "range"},
    "KXSOLE":       {"taker_fee": 0.07,  "maker_mult": 0.0175, "poll_every": 6, "contract_type": "range"},
}
```

---

## DB Schema Reference

```sql
-- Arb scanner tables
scans (id, timestamp, series_ticker, expiry_window, num_strikes, scan_duration_ms)
ladder_snapshots (id, timestamp, series_ticker, expiry_window, strike, yes_ask, yes_bid, no_ask, no_bid, yes_depth, no_depth)
opportunities (id, timestamp, series_ticker, expiry_window, opp_type, sub_type, strike_low, strike_high, yes_ask_low, no_ask_high, combined_cost, estimated_profit, estimated_profit_maker, btc_price_at_detection, time_to_expiry_seconds, depth_thin_side)
trades (id, timestamp, expiry_window, opp_type, strike_low, strike_high, leg1_side, leg1_price, leg1_fill_status, leg2_side, leg2_price, leg2_fill_status, orphaned, exit_price, realized_pnl, fees)
arb_stability (id, timestamp, series_ticker, expiry_window, strike_low, strike_high, combined_cost, depth_thin_side, first_seen, scan_count, status, close_reason)

-- Market maker tables
mm_quotes (id, timestamp, ticker, side, price, size, action)
mm_fills (id, timestamp, ticker, side, price, count, inventory_after, realized_pnl_cumulative)
mm_snapshots (id, timestamp, cycle, ticker, strike, bid_price, ask_price, inventory, strike_realized_pnl, total_realized_pnl)
```

---

## Next Steps

### MM Live Test (current — started 2026-02-26)
- [x] Set `MM_CONFIRM=true`, `MM_QUOTE_SIZE=2`
- [x] Real orders resting on Kalshi (post_only confirmed)
- [x] Fill detection bug found and fixed (stranded positions)
- [x] Position reconciliation deployed and verified
- [x] Exit-only strike management deployed (stable quoting)
- [x] Multi-expiry tiers deployed (hourly > daily > weekly)
- [x] Sports feasibility scan completed (not viable)
- [ ] Monitor fills and P&L — currently unwinding stranded inventory (+2 T66499, +2 T68499)
- [ ] Observe hourly event quoting behavior (books may be empty)
- [ ] After inventory cleared + 24h stable: increase MM_QUOTE_SIZE to 5
- [ ] Fund account to $100+ before scaling up

### Observations & Issues
- Hourly events (42min TTL) found but books often empty (bid=0 ask=0)
- Daily events have 2c spreads (below MM_MIN_BOOK_SPREAD=3c)
- Most fills happening on daily/weekly events with wider books
- May need to lower MM_MIN_BOOK_SPREAD or adjust tier preference

### Dashboard
- [ ] Deploy Streamlit dashboard as second systemd service
- [ ] Add MM-specific dashboard page (inventory, P&L, quote history)
- [ ] Add spread/vol charts from mm_snapshots data

### Future Improvements
- [ ] Websocket feed instead of polling orderbooks (reduce API calls)
- [ ] Multi-series MM (KXETHD, KXSOLD) — same above/below structure
- [ ] Order flow imbalance detection (requires months of fill data)
- [ ] Cross-strike inventory hedging (requires scale past $500/day)
- [ ] Investigate Economics/Weather markets for MM opportunities

---

## Binary Arb Mode (MODE=binary_arb) — KXBTC15M

### Overview
Third mode added 2026-02-26. Scans KXBTC15M 15-minute binary contracts for same-market yes+no mispricing (combined ask < $1.00). When found, buys both sides to lock in risk-free profit. Based on friend's strategy (see `plan__1_.md` prospectus).

### Friend's Prospectus Claims
- Starting balance $60,066, net gain ~$228 over 4 days (Feb 22–26)
- 25 contracts per trade, threshold ≤96c
- Opportunities appear 2–8 times per 15-minute window during active hours
- Clean double-fills profitable; one-sided fills are primary loss source
- 8-second hedge delay to check fill status, unwind one-sided fills

### Files Added/Modified
| File | Change |
|------|--------|
| `binary_arb_bot.py` | **NEW** — BinaryArbBot class, scan loop, execution, hedge checks |
| `config.py` | Added `binary_arb` to READ_ONLY exclusion, KXBTC15M to SERIES, 4 new env vars |
| `db.py` | Added `binary_arb_trades` table + indexes |
| `db_logger.py` | Added `log_binary_arb_trade()` and `update_binary_arb_trade()` |
| `main.py` | Added binary_arb branch, banner, `load_dotenv()` before config import, non-fatal balance check in READ_ONLY |
| `investigate_kxbtc15m.py` | Investigation script for market structure + long-duration orderbook scanning |

### Config Variables
```python
BINARY_ARB_THRESHOLD = int(os.getenv("BINARY_ARB_THRESHOLD", "96"))   # Max combined yes+no to trigger
BINARY_ARB_SIZE = int(os.getenv("BINARY_ARB_SIZE", "10"))             # Contracts per side
BINARY_ARB_COOLDOWN = int(os.getenv("BINARY_ARB_COOLDOWN", "30"))     # Seconds between trades on same ticker
BINARY_ARB_HEDGE_DELAY = int(os.getenv("BINARY_ARB_HEDGE_DELAY", "8")) # Seconds before checking fills
```

### READ_ONLY / Dry-Run Support
- Added `READ_ONLY` env var override: `os.getenv("READ_ONLY", "").lower() in ("true", "1")`
- Allows `MODE=binary_arb` + `READ_ONLY=true` for observation without trading
- Dry-run prints every market's orderbook to stdout: ticker, yes_ask, no_ask, combined, depths, WOULD TRADE flag
- Dry-run logs opportunities to `binary_arb_trades` with `hedge_action='dry_run'`
- Balance check made non-fatal in READ_ONLY (demo API key can read prod market data but not portfolio)

### Bug Fixed: load_dotenv() ordering
- `load_dotenv()` was called inside `main()` but `import config` happened at module top level
- .env vars (MODE, READ_ONLY) weren't loaded when config.py evaluated
- Fix: moved `load_dotenv()` before `import config` at top of main.py

### Dry-Run Results (2026-02-26, ~22:47–23:01 UTC)

#### Market Structure
- **1 event, 1 market per 15-min window** — no overlapping windows, no multiple strikes
- Ticker format: `KXBTC15M-{date}{HHMM}-{suffix}` (e.g., `KXBTC15M-26FEB261800-00`)
- `market_type: binary`, `strike_type: greater_or_equal`
- Question: "BTC price up in next 15 mins?" with a floor_strike (price to beat)
- 15-min windows: open_time to close_time, settlement 60s after close
- 7,018 settled events (huge history) — market has been running a while

#### Orderbook Observations (~200 scans across 2 windows, 1s and 5s polling)
- **Combined cost range: 101–108** (never below 101)
- **Average: ~102**
- **Zero opportunities at threshold ≤96** in entire observation period
- Near expiry (last 60s of window): spread widens to 104–108, books thin
- During window transition: ~15s gap with empty books before new window opens
- New window opens with fresh price discovery near 50/50, combined ~101–103
- Depths vary wildly: 1–11,000 contracts, median ~200–500
- Price swings are real (yes went 62→5→28 in one window as BTC dropped) but spread stays 101+

#### Assessment vs Friend's Claims
- Friend claims 2–8 opps per window at ≤96c. We saw 0 in ~200 scans.
- **Possible explanations:**
  1. Friend runs on low-latency VPS — sub-second opportunities we can't catch even at 1s polling
  2. Market conditions during our scan (Wed evening EST) may differ from friend's active period (weekday daytime)
  3. The 1–2c spread (combined=101–102) may compress to 100 or below only during volatile moments we haven't captured yet
  4. Friend's $228 over 4 days with $60k capital = 0.38% return — this is very thin, consistent with rare fleeting opportunities
- **Need longer observation**: run on VPS at 1s polling for 24h+ to capture daytime volatility
- **The spread floor of 101 is suspicious** — may indicate professional MMs keeping it above 100 with 1c edge

### Next Steps (Binary Arb)
- [ ] Run 24h+ scan from DigitalOcean droplet (lower latency, 1s polling)
- [ ] Analyze results: does combined ever dip ≤98 during US market hours?
- [ ] If no opportunities found: strategy may not be viable at our latency
- [ ] If opportunities found: tune threshold, test with small size (5 contracts)
- [ ] Consider: friend may have different fee tier or API access level
