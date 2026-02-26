# Kalshi Multi-Series Arb Bot — Project Plan

## Current Status (2026-02-25)

### What's Built & Running
The bot is **live on production** (`api.elections.kalshi.com`) in **READ-ONLY mode** on a DigitalOcean droplet (`159.65.44.106`). It scans **8 series** (5 above/below arb-eligible + 3 range monitoring-only) every cycle, detecting cross-strike arbitrage opportunities and logging everything to SQLite. Stale/phantom quotes are filtered out but tracked separately. **~1,200 markets** scanned per cycle across above/below series.

**Phase 1 restarted 2026-02-25** on correct contract type (KXBTCD/KXETHD/KXSOLD above/below). Prior data on KXBTC/KXETH/KXSOLE range contracts is invalid for cross-strike arb — range bucket yes_asks sum to ~400-500c (not ~100c), confirming zero full-ladder arb potential on range contracts.

### Architecture
```
Bot (systemd: kalshi-bot)         Dashboard (not yet deployed)
    bot.py -> scanner.py              dashboard.py (Streamlit)
    db_logger.py (write)              queries.py (read)
         \                           /
          -----> arb_bot.db <-------
                (WAL mode)
```
Two processes, one SQLite DB. WAL mode + busy_timeout prevents contention.

### File Inventory

| File | Purpose | Status |
|------|---------|--------|
| `auth.py` | RSA-PSS signing, authenticated requests | Done |
| `config.py` | All tuning params, per-series fees, READ_ONLY flag | Done |
| `kalshi_api.py` | API wrappers (events->markets, orderbook, orders) | Done |
| `scanner.py` | Ladder builder, stale filter, opportunity detector (Type A/B/C) | Done |
| `bot.py` | Main scan loop, multi-series, maker profit, execution logic | Done |
| `main.py` | Entry point, env validation, startup prints | Done |
| `db.py` | SQLite schema (4 tables), migrations, connection helpers | Done |
| `db_logger.py` | Write helpers + maker summary query | Done |
| `queries.py` | SQL queries for dashboard (returns DataFrames) | Done (needs series_ticker update) |
| `dashboard.py` | 5-page Streamlit app (Overview, Ladder, Matrix, Trades, Signals) | Done (not deployed) |
| `test_markets.py` | Diagnostic script for API/market discovery | Done |
| `deploy.sh` | Deployment script (systemd setup) | Exists (needs dashboard service) |

### Deployment Details
- **Droplet**: `ssh root@159.65.44.106`
- **Bot path**: `/opt/kalshi-arb-bot`
- **Repo**: `https://github.com/nstef18447/kalshi-arb-bot-.git`
- **Service**: `systemd: kalshi-bot` (auto-restart on failure)
- **Env**: `.env` with `KALSHI_ENV=prod`, `READ_ONLY=true`, prod API key + RSA private key
- **API**: `https://api.elections.kalshi.com/trade-api/v2` (production)

### Series Being Scanned

#### Above/below contracts — arb-eligible (cross-strike logic valid)
| Series | Underlying | Type | ~Markets | Durations | Schedule |
|--------|-----------|------|----------|-----------|----------|
| **KXBTCD** | Bitcoin | Above/below | 165 | 1h / 25h / 169h | 24/7 |
| **KXETHD** | Ethereum | Above/below | 165 | 1h / 25h / 169h | 24/7 |
| **KXSOLD** | Solana | Above/below | 200 | 1h / 25h / 169h | 24/7 |
| **KXINXU** | S&P 500 | Above/below | 60 | 25h | US market hours |
| **KXNASDAQ100U** | Nasdaq-100 | Above/below | 60 | 25h | US market hours |

#### Range contracts — monitoring only (arb logic does NOT apply)
| Series | Underlying | Type | ~Markets | Note |
|--------|-----------|------|----------|------|
| KXBTC | Bitcoin | Range | 165 | Polled every 6th cycle |
| KXETH | Ethereum | Range | 165 | Polled every 6th cycle |
| KXSOLE | Solana | Range | 200 | Polled every 6th cycle |

### Key Decisions Made
- **Above/below vs Range**: Cross-strike arb logic (buy Yes_low + No_high) only works on above/below contracts (`strike_type: greater`). Range contracts have independent buckets — buying Yes on two adjacent ranges doesn't guarantee a payout. The `contract_type` field in config gates arb detection.
- **KXBTCD/KXETHD/KXSOLD**: The above/below tickers are `KXBTCD`, `KXETHD`, `KXSOLD` (the "D" suffix = directional). These are the primary arb targets.
- **KXBTC15M is useless**: `KXBTC15M` is a single up/down market (1 contract per event) — no multi-strike ladder for cross-strike arbs.
- **No 15-minute contracts exist**: The shortest multi-strike windows are 1 hour. Prospectus "15-minute" claims were incorrect.
- **Events->Markets two-step**: Direct `series_ticker` query on `/markets` misses markets. Must fetch events first, then markets per event.
- **Defense-in-depth READ_ONLY**: Guards at bot logic layer (skips execution) AND API layer (RuntimeError on write calls).
- **Production URL**: `api.elections.kalshi.com` is correct. `trading-api.kalshi.com` returns 401 redirect notice.
- **SQLite WAL mode**: Allows concurrent reads from dashboard while bot writes.
- **DB migration ordering**: Schema split into SCHEMA_TABLES + migrations + SCHEMA_INDEXES to avoid "no such column" errors on existing DBs.
- **Stale quote filter**: Depth < 10 or combined spread > 110 indicates phantom/stale quotes — logged separately, not counted as real opportunities.
- **close_time vs expiration_time**: Markets have `close_time` (when trading stops) and `expiration_time` (settlement ~1 week later). Use `close_time` for grouping ladders and time_to_expiry.
- **Market durations**: Events are hourly (1h), daily (25h), or weekly (169h). All are valid arb targets for above/below contracts.
- **Per-series taker fees**: Execution uses per-series `taker_fee` from config, not a global `FEE_RATE`. Both taker and maker use parabolic formula `mult * P * (1-P)`.
- **Equity index poll rate**: KXINXU/KXNASDAQ100U scan every 6th cycle (30s) since they show no actionable opps — saves API calls.
- **Phase 1 data on KXBTC/KXETH/KXSOLE is invalid**: All prior "arb" detections were on range contracts where the cross-strike logic doesn't apply. Phase 1 effectively restarts on KXBTCD/KXETHD/KXSOLD.
- **Range contracts confirmed dead for arbs**: Full-ladder analysis on KXBTC shows complete ladders (24-27 strikes) sum to ~400-500c yes_ask, not ~100c. Range buckets are independently priced with wide spreads. Zero full-ladder arbs exist.

---

## Execution Logic (v2)

### Pre-flight liquidity check
- Both sides must have >= `MIN_DEPTH` (30) contracts at best ask before placing any orders

### Leg the illiquid side first
- Compare depth on both sides, place the thinner side first
- Wait up to `FIRST_LEG_TIMEOUT` (2s) for fill
- If first leg doesn't fill -> cancel and abort with zero exposure
- If it fills -> immediately place second leg

### Orphan fill recovery
- If second leg fails to fill within `SECOND_LEG_TIMEOUT` (3s):
  - Cancel unfilled second leg
  - Sell filled first leg at best bid immediately
  - Log ticker, side, fill price, exit price, realized P&L

### Circuit breaker
- Rolling window of last `WINDOW_SIZE` (20) attempts
- If orphan rate > `MAX_ORPHAN_RATE` (25%) -> pause for `COOLDOWN_MINUTES` (10)

### Opportunity Types
- **Type A (monotonicity)**: Adjacent strikes where yes_ask doesn't decrease monotonically
- **Type B (probability_gap)**: Probability gaps between strikes larger than expected
- **Type C (hard_arb)**: yes_ask + no_ask < 100 across strike pair (guaranteed profit)
- **Type C (soft_arb)**: Combined cost close to 100, marginal after fees but positive EV

### Stale Quote Filter
Strikes are marked stale and excluded from real opportunities if:
- Depth on either side < `MIN_QUOTE_DEPTH` (10 contracts)
- `yes_ask + no_ask > MAX_COMBINED_SPREAD` (110)

Stale violations are logged separately as `stale_A_monotonicity`, `stale_B_probability_gap`, `stale_C_arb` counts.

---

## Config Variables
```python
READ_ONLY = True              # Env var, defaults true — no trading
ARB_THRESHOLD = 95            # Max combined price in cents
MAX_CONTRACTS = 25            # Contracts per leg
POLL_INTERVAL = 5             # Seconds between scan cycles

SERIES = {
    # Above/below — arb-eligible
    "KXBTCD":       {"taker_fee": 0.07,  "maker_mult": 0.0175, "poll_every": 1, "contract_type": "above_below"},
    "KXETHD":       {"taker_fee": 0.07,  "maker_mult": 0.0175, "poll_every": 1, "contract_type": "above_below"},
    "KXSOLD":       {"taker_fee": 0.07,  "maker_mult": 0.0175, "poll_every": 1, "contract_type": "above_below"},
    "KXINXU":       {"taker_fee": 0.035, "maker_mult": 0.00875, "poll_every": 6, "contract_type": "above_below"},
    "KXNASDAQ100U": {"taker_fee": 0.035, "maker_mult": 0.00875, "poll_every": 6, "contract_type": "above_below"},
    # Range — monitoring only
    "KXBTC":        {"taker_fee": 0.07,  "maker_mult": 0.0175, "poll_every": 6, "contract_type": "range"},
    "KXETH":        {"taker_fee": 0.07,  "maker_mult": 0.0175, "poll_every": 6, "contract_type": "range"},
    "KXSOLE":       {"taker_fee": 0.07,  "maker_mult": 0.0175, "poll_every": 6, "contract_type": "range"},
}

MAX_EXPOSURE = 50000_00       # Max capital in cents ($50k)
MIN_DEPTH = 30                # Min contracts at best ask
FIRST_LEG_TIMEOUT = 2         # Seconds for illiquid leg fill
SECOND_LEG_TIMEOUT = 3        # Seconds for second leg fill
WINDOW_SIZE = 20              # Rolling window for orphan rate
MAX_ORPHAN_RATE = 0.25        # Circuit breaker threshold
COOLDOWN_MINUTES = 10         # Pause duration on trip
SNAPSHOT_CACHE_SIZE = 12      # 12 x 5s = 60s history
SOFT_ARB_PROB_THRESHOLD = 0.60
```

### Stale Filter Constants (scanner.py)
```python
MIN_QUOTE_DEPTH = 10          # Ignore strikes with depth < 10 on either side
MAX_COMBINED_SPREAD = 110     # Ignore strikes where yes_ask + no_ask > 110
```

---

## Maker Fee Analysis

### Fee Formulas
- **Taker fee**: `taker_fee * payout` (flat % on $1 payout per leg)
- **Maker fee**: `maker_mult * P * (1-P) * 100` cents per leg, where P = price/100

### Per-Series Rates
| Series | Taker Fee | Maker Mult | Example: 40c leg maker fee |
|--------|-----------|------------|---------------------------|
| KXBTCD | 7.0% | 0.0175 | 0.42c |
| KXETHD | 7.0% | 0.0175 | 0.42c |
| KXSOLD | 7.0% | 0.0175 | 0.42c |
| KXINXU | 3.5% | 0.00875 | 0.21c |
| KXNASDAQ100U | 3.5% | 0.00875 | 0.21c |

### 30-Minute Summary
Every 30 minutes the bot logs a maker fee analysis:
- Total hard arbs detected
- How many profitable at taker fees vs maker fees
- Average gross spread and depth for maker-profitable opportunities
- Breakdown per series + ALL aggregate

---

## Phase 1: Validation (Current)

**Goal**: Answer "do cross-strike arb opportunities actually exist, how often, and how fat?"

### Completed
- [x] Bot scanning production orderbooks in READ_ONLY mode
- [x] Multi-series support (5 series: 3 crypto + 2 equity index) with per-series fees
- [x] Correct series ticker discovery (KXBTC, KXETH, KXSOLE, KXINXU, KXNASDAQ100U)
- [x] SQLite logging (scans, snapshots, opportunities, trades tables)
- [x] `series_ticker` column in all DB tables with migration support
- [x] `estimated_profit_maker` column for maker fee profit tracking
- [x] Stale quote filter (depth < 10, combined > 110) with separate logging
- [x] 30-minute maker fee summary (taker vs maker profitability)
- [x] db_logger wired into bot scan loop with try/except (logging failures never crash bot)
- [x] Dashboard code written (5 pages with plotly charts)
- [x] Phase 1 analysis report generated (`phase1_report.md`)
- [x] Taker fee formula fixed: parabolic `mult * P * (1-P)` per leg (was flat 7c)
- [x] time_to_expiry fixed: uses `close_time` (trading stops) not `latest_expiration_time` (settlement)
- [x] `config.FEE_RATE` references fixed: execution path uses per-series taker_fee
- [x] Series ticker added to violation log lines (`[KXBTC] Violations: ...`)
- [x] Orderbook stability tracking: `arb_stability` table tracks arb persistence across scans
- [x] Scan cycle timing: breakdown every 10 cycles (api/ladder/detect+db per series)
- [x] Per-series poll intervals: KXINXU/KXNASDAQ100U poll every 6th cycle (30s)
- [x] **CRITICAL FIX**: Discovered KXBTC/KXETH/KXSOLE are RANGE contracts, not above/below. Arb logic is invalid for range contracts.
- [x] Added KXBTCD/KXETHD/KXSOLD (above/below contracts) as primary arb targets
- [x] Added `contract_type` field to config: "above_below" = arb detection, "range" = monitoring only
- [x] Range contracts demoted to poll_every=6, no arb detection
- [x] **Phase 1 effectively restarted** on correct contract type (2026-02-25)

### Remaining
- [ ] **Collect 24-48h of KXBTCD/KXETHD/KXSOLD data** — fresh Phase 1 on above/below contracts
- [ ] **Deploy Streamlit dashboard** to droplet as second systemd service
  - Add `kalshi-dashboard` service: `streamlit run dashboard.py --server.port 8501 --server.headless true --server.address 0.0.0.0`
  - Open firewall: `ufw allow 8501/tcp`
  - Access at `http://159.65.44.106:8501`
- [ ] **Update queries.py** — add series_ticker filtering to dashboard queries
- [ ] **Review dashboard** — check if opportunities are real, assess spread sizes, persistence, depth
- [ ] **Add DB_RETENTION_DAYS** to config.py (cleanup old rows, suggested 7 days)
- [ ] **Re-run Phase 1 analysis** on KXBTCD/KXETHD/KXSOLD data once 24h+ accumulated
- [ ] **Update prospectus.md** — remove all "15-minute" references, correct contract types

---

## Phase 2: Execution (Future)

**Goal**: Enable live trading once validation confirms profitable opportunities exist.

### Prerequisites
- Phase 1 data shows consistent arb opportunities with sufficient spread after fees
- Maker fee analysis shows positive expected profit (maker fees much lower than taker)
- Orderbook depth > MIN_DEPTH (30 contracts) on both sides
- Opportunities persist long enough for sequential leg placement

### Steps
- [ ] Set `READ_ONLY=false` in `.env` (bot already has all execution logic)
- [ ] Start with small size: `MAX_CONTRACTS=5`, `MAX_EXPOSURE=500_00` ($500)
- [ ] Monitor orphan rate via dashboard (circuit breaker trips at 25%)
- [ ] Tune `FIRST_LEG_TIMEOUT` and `SECOND_LEG_TIMEOUT` based on fill data
- [ ] Gradually scale up contracts and exposure based on P&L
- [ ] Consider maker orders for better fees (significant cost reduction)

---

## DB Schema Reference

```sql
-- scans: one row per scan cycle per series
(id, timestamp, series_ticker, expiry_window, num_strikes, scan_duration_ms)

-- ladder_snapshots: one row per strike per scan
(id, timestamp, series_ticker, expiry_window, strike,
 yes_ask, yes_bid, no_ask, no_bid, yes_depth, no_depth)

-- opportunities: detected arb opportunities
(id, timestamp, series_ticker, expiry_window, opp_type, sub_type,
 strike_low, strike_high, yes_ask_low, no_ask_high, combined_cost,
 estimated_profit, estimated_profit_maker,
 btc_price_at_detection, time_to_expiry_seconds, depth_thin_side)

-- trades: execution attempts (empty until Phase 2)
(id, timestamp, expiry_window, opp_type, strike_low, strike_high,
 leg1_side, leg1_price, leg1_fill_status, leg2_side, leg2_price,
 leg2_fill_status, orphaned, exit_price, realized_pnl, fees)

-- arb_stability: tracks arb persistence across consecutive scans
(id, timestamp, series_ticker, expiry_window, strike_low, strike_high,
 combined_cost, depth_thin_side, first_seen, scan_count, status, close_reason)
```

## Dashboard Pages
1. **Overview** — metric cards, opps/hour stacked bar, spread histogram, spread-vs-expiry scatter
2. **Ladder Explorer** — live strike table with conditional formatting, yes-price chart, heatmap
3. **Cross-Strike Matrix** — NxN heatmap of combined costs with timestamp scrubber
4. **Trade Log** — equity curve, rolling orphan rate, filterable table (Phase 2)
5. **Competition Signals** — persistence trends, spread compression, flash opps, time-of-day breakdown
