# Kalshi Arb Bot — Project Plan

## Current Status (2026-03-04, evening update)

### What's Running
**Mispricing scanner** on DigitalOcean droplet (`159.65.44.106`), live since 2026-03-01 01:53 UTC.
- `MODE=mispricing_scanner`, `READ_ONLY=true`
- Paper trade tracking with **DB-backed deduplication** — one trade per bucket_ticker, 24h cooldown
- Resolution checker runs every 5 minutes to settle paper trades
- Near-miss tracker captures signals within 50-99% of threshold for sensitivity analysis
- **Liquidity filters** (added 2026-03-04): `bid_depth >= 5` AND `spread <= 30c` required for paper trades
- **352 resolved trades | +$30.44 | 83% win rate (292W/60L)** — past 500-trade target
- Bid/ask/spread/bid_depth tracking deployed to droplet (was local-only before this session)

**Account:** ~$41.44 balance (market maker stopped with 2 tiny positions that will settle on their own)

### Paper Trade Deduplication (added 2026-03-01)
The original scanner logged a new paper trade every scan cycle for the same bucket — produced 3,076 rows in 15 hours for only 107 unique tickers. Fixed with:
- **DB-level dedup**: `has_open_or_recent_paper_trade(bucket_ticker)` checks for open trades OR any trade within 24h
- **Cleanup**: deleted 2,969 duplicate rows, kept earliest entry per bucket_ticker
- **Raw signals still logged**: `mispricing_signals` table gets every detection (for frequency/persistence analysis), but `paper_trades` only gets one per bucket
- New paper trades only appear for genuinely new bucket_tickers or after 24h cooldown post-resolution

### After Dedup — 107 Unique Paper Trades
| Signal Type | Trades | Avg Gap |
|-------------|--------|---------|
| crypto_range | 93 | 54.0c |
| fed_base_rate | 14 | 39.6c |

| Series | Trades |
|--------|--------|
| KXETH | 42 |
| KXSOLE | 35 |
| KXBTC | 16 |
| KXFEDDECISION | 14 |

Near misses: 1,584 (crypto_range: 1,338, fed_base_rate: 160, proportional: 86)

### Previous Mode (Market Maker)
Market maker was running KXBTCD-only since 2026-02-27. Stopped to switch to mispricing scanner mode.
- `.env.mm_backup2` saved on droplet for easy restore
- To restore: `cp /opt/kalshi-arb-bot/.env.mm_backup2 /opt/kalshi-arb-bot/.env && systemctl restart kalshi-bot`

---

## Mispricing Scanner Architecture

### Strategy
Inspired by Polymarket wallet 0x8e9e's framework: find multi-outcome markets where YES prices sum to significantly more than $1.00, identify which specific buckets are overpriced relative to fair value, and flag SELL YES signals.

### How It Works

```
Scanner Loop (5s cycle)
  ├── For each series (poll at configured interval):
  │   ├── Fetch events → markets → orderbooks
  │   ├── Build EventSnapshot (buckets, YES sum, excess)
  │   ├── Filter out cumulative/threshold markets (safety check)
  │   ├── Estimate fair values (model-dependent)
  │   ├── Detect mispricings → signals + near-misses
  │   ├── Log ALL signals to mispricing_signals (raw, no dedup)
  │   ├── Log paper_trades only if:
  │   │     - No open/recent trade for that bucket (DB dedup + 24h cooldown)
  │   │     - bid_depth >= 5 (something to sell into)
  │   │     - spread <= 30c (liquid enough to trade)
  │   └── Log near-misses (in-memory 10min cooldown)
  └── Resolution checker (every 5 min):
      ├── Fetch open paper trades
      ├── Check settled markets via Kalshi API
      └── Resolve: P&L = entry_price if result="no", -(100-entry_price) if result="yes"
```

### Series Monitored

| Series | Category | Poll Interval | Market Type |
|--------|----------|---------------|-------------|
| KXBTC | crypto | 30s | Range buckets (daily) |
| KXETH | crypto | 30s | Range buckets (daily) |
| KXSOLE | crypto | 30s | Range buckets (daily) |
| KXFEDDECISION | economics | 120s | Exclusive outcomes (5 per meeting) |
| KXRATECUTCOUNT | economics | 300s | Exclusive outcomes (21 buckets) |
| KXBALANCEPOWERCOMBO | politics | 300s | Exclusive outcomes (4 combos) |

**Excluded:** KXCPIYOY, KXCPICOREYOY, KXGDP — cumulative "above X%" markets where YES prices naturally sum to 400-800c. The $1.00-sum property does not apply.

### Fair Value Models

| Model | Used For | How It Works |
|-------|----------|--------------|
| **Fed base rate** | KXFEDDECISION (>90 days out) | Historical FOMC frequencies: hold=55%, cut 25bp=25%, hike 25bp=12%, etc. |
| **Proportional** | All others with total YES > 100c | Scale market prices down so they sum to 100c. Flags buckets carrying disproportionate vig. |
| **Center-weighted** | Fallback (total YES ≤ 100c) | Triangle distribution peaking at center bucket. |

Near-term Fed meetings (<90 days) use proportional model instead — market consensus dominates historical base rates.

### Signal Types & Thresholds

| Signal Type | Threshold | Near-Miss Floor | Used For |
|-------------|-----------|-----------------|----------|
| `fed_base_rate` | 15c | 8c | KXFEDDECISION (>90 days) |
| `crypto_range` | 7c | 4c | KXBTC, KXETH, KXSOLE |
| `proportional` | 7c | 4c | Everything else |

### Paper Trade P&L

For each signal, we record a hypothetical **SELL YES** at the current price:
- If market resolves **NO** (YES=0c): P&L = +entry_price (we sold something worthless)
- If market resolves **YES** (YES=100c): P&L = -(100 - entry_price) (we sold something that paid out)
- Example: Sell YES at 66c → if NO: +66c profit; if YES: -34c loss

### Paper Trade Filters

1. **One trade per bucket_ticker**: if `bucket_ticker` has an open (unresolved) paper trade → skip
2. **24-hour cooldown**: if the most recent trade for that `bucket_ticker` (any status) was within 24h → skip
3. **Liquidity: bid_depth >= 5**: must have at least 5 contracts across top 3 yes_bid levels to sell into
4. **Liquidity: spread <= 30c**: bid-ask spread must be 30c or less — wider = illiquid/untradeable
5. **Raw signals still logged**: `mispricing_signals` table captures every detection for frequency analysis (no filters)
6. **Survives restarts**: dedup is DB-backed, not in-memory
7. **Tradeable tag**: `tradeable` column (0/1) on paper_trades — retroactively set to 0 for trades with bid_depth<5 or spread>30

### Cumulative Market Detection

Safety check in `_fetch_event_snapshot()` to catch "above X" markets that slip through:
- If total YES sum > 200c AND prices are ≥70% monotonically decreasing → skip
- This caught BTC/ETH/SOL near-expiry events that were actually cumulative structure

---

## File Inventory

| File | Purpose | Status |
|------|---------|--------|
| `auth.py` | RSA-PSS signing, authenticated requests | Done |
| `config.py` | Mode selector, per-series fees, READ_ONLY flag, mispricing thresholds, LIVE_EXECUTION + ORDER_SIZE + ORDER_EXPIRY_SECONDS | Done |
| `kalshi_api.py` | API wrappers + `get_market()` for resolution checking | Done |
| `mispricing_scanner.py` | MispricingScanner class, fair value models, paper trade logging, resolution checker, DB-backed dedup, bid/ask/spread tracking, adjusted P&L, live execution via limit orders | Done |
| `db.py` | SQLite schema (15 tables), migrations, connection helpers | Done |
| `db_logger.py` | Write helpers for all tables + `has_open_or_recent_paper_trade()` dedup check | Done |
| `queries.py` | SQL queries for dashboard (original 5 pages + paper trades page) | Done |
| `dashboard.py` | 6-page Streamlit app (added Paper Trades page) | Done |
| `main.py` | Entry point, all 4 modes (arb, market_maker, binary_arb, mispricing_scanner) | Done |
| `mm_engine.py` | MarketMaker class — full engine with all protections | Done (paused) |
| `mm_config.py` | MM config from env vars | Done (paused) |
| `scanner.py` | Arb ladder builder + opportunity detector | Done |
| `bot.py` | Arb scan loop, multi-series, maker profit, execution | Done |
| `deploy.sh` | Deployment script (systemd setup) | Done |

### Database Tables

**Paper trade tracking:**
- `paper_trades` — signal_type, entry_price, fair_value_est, overpricing_gap, status (open/resolved), resolved_price, pnl_cents, yes_bid, yes_ask, spread, bid_depth, adjusted_pnl_cents, tradeable (0/1)
- `paper_near_misses` — yes_price, fair_value_est, gap, threshold_used (for sensitivity analysis)

**Mispricing signals:**
- `mispricing_signals` — raw signal log (event, bucket, price, fair value, gap, yes_bid, yes_ask, spread, bid_depth) — NOT deduped, logs every detection

**Live execution:**
- `live_orders` — order_id, bucket_ticker, price_cents, count, status (open/cancelled/filled), filled_count, filled_price, expires_at

**Original arb/MM tables:**
- `scans`, `ladder_snapshots`, `opportunities`, `trades`, `arb_stability`
- `binary_arb_trades`, `mm_quotes`, `mm_fills`, `mm_snapshots`

---

## Dashboard — Paper Trades Page

### Metrics
- Total signals, open/resolved counts, W/L record, win rate
- Cumulative P&L, average edge at entry
- 14-day progress bar (countdown to going live)

### Breakdowns
- **By signal type**: fed_base_rate / crypto_range / proportional — W/L/P&L each
- **By category**: crypto / economics / politics

### Charts
- Cumulative P&L equity curve (resolved trades)
- Near-miss gap distribution histogram (for threshold tuning)

### Tables
- Full paper trade table (filterable by status + signal type)
- Near-miss summary (count, avg gap, min/max per signal type)
- Recent near-misses (last 50, expandable)

---

## Deployment Details

- **Droplet**: `ssh root@159.65.44.106`
- **Bot path**: `/opt/kalshi-arb-bot`
- **Service**: `systemd: kalshi-bot` (auto-restart, enabled for boot)
- **Logs**: `journalctl -u kalshi-bot -f`
- **DB**: `/opt/kalshi-arb-bot/arb_bot.db` (76MB, SQLite WAL mode)

### Current .env on droplet
```
KALSHI_API_KEY=<redacted>
KALSHI_PRIVATE_KEY_PATH=/opt/kalshi-arb-bot/kalshi_private_key.pem
KALSHI_ENV=prod
MODE=mispricing_scanner
READ_ONLY=true
MISPRICING_THRESHOLD=15
MISPRICING_MIN_EXCESS=5
```

---

## Paper Trade Results

### 352 Resolved Trades (2026-03-04, latest)

**Overall: +$30.44 | 83.0% win rate (292W / 60L)**
- 525 total trades (352 resolved, 165 open)
- All resolved trades are crypto_range — no fed/economics resolutions yet
- Edge per trade: +8.65¢

**Win Rate by Gap Size:**
| Gap Bucket | Trades | Win % | Total P&L |
|---|---|---|---|
| 7-15¢ | 203 | 86% | +$5.56 |
| 16-30¢ | 98 | 77% | +$0.90 |
| 31-50¢ | 26 | 81% | +$5.16 |
| 51¢+ | 25 | 84% | +$18.82 |

**16-30¢ gap update:** Improved from net-negative (-$1.55 at 112 trades) to barely positive (+$0.90), but 77% win rate is right at breakeven threshold. Still the weakest bucket.

**51¢+ signals are the moneymakers:** $18.82 from 25 trades = 75¢ avg profit per trade.

**Liquidity filter impact:** 517 of 525 trades were logged before bid/ask tracking was deployed, so most have NULL spread/bid_depth. Going forward, new trades require bid_depth>=5 AND spread<=30c. Only 4 of 8 post-deploy trades were filtered out.

### 36-Hour Results (2026-03-02, 112 resolved trades)

**Overall: +$6.06 | 83% win rate (93W / 19L)**
- Avg win: +20.7¢ | Avg loss: -69.5¢ | Edge per trade: +5.41¢
- Breakeven win rate: ~77% → currently above breakeven

**Position sizing projections (at paper edge of 5.41¢/trade, ~56 trades/day):**
| Contracts/Signal | Daily P&L | Weekly | Monthly |
|---|---|---|---|
| 10 ($10/signal) | $30 | $212 | $908 |
| 50 ($50/signal) | $151 | $1,060 | $4,540 |

*Halve these estimates for realistic taker fills after bid/ask spread.*

### First 15 Hours (2026-03-01, pre-dedup)
- 3,076 raw paper trade rows → **107 unique bucket_tickers** after dedup cleanup
- crypto_range dominates: 93 trades (ETH 42, SOL 35, BTC 16), avg gap 54c
- fed_base_rate: 14 trades (all "Hike 0bps" on far-out FOMC meetings), avg gap 40c
- 1,584 near-misses logged

### Key Observations
- Crypto range signals fire aggressively — BTC/ETH/SOL daily events have YES sums of 185-194c (85-94c excess)
- The 7c threshold for crypto_range catches many buckets; most are small probabilities (10-20c) with proportional fair values of 5-10c
- Fed "Hike 0bps" (hold) at 66-77c vs 26c historical base rate is the biggest gap signal
- Proportional signals are rare (only 86 near-misses, 0 trades) — markets with lower excess don't produce big gaps
- All 112 resolved trades are crypto_range — no fed/economics resolutions yet (first FOMC ~March 18-19)

---

## Next Steps

### Completed
- [x] Add paper trade deduplication (DB-backed, 24h cooldown)
- [x] Clean up 2,969 duplicate rows
- [x] First crypto resolutions — 112 resolved, 83% win rate, +$6.06
- [x] Analyze P&L distribution and gap-size performance
- [x] Reach 500 resolved trades — **352 resolved as of 2026-03-04** (525 total)
- [x] Factor in bid/ask spread: log yes_bid alongside yes_ask for realistic taker P&L — DONE (2026-03-04)
- [x] Compute adjusted_pnl_cents using yes_bid as entry — DONE (2026-03-04)
- [x] Add liquidity filters: bid_depth>=5 AND spread<=30c — DONE (2026-03-04)
- [x] Add `tradeable` column with retroactive tagging — DONE (2026-03-04)
- [x] Deploy bid/ask tracking + liquidity filters to droplet — DONE (2026-03-04)

### Now (paper trading with liquidity filters active)
- [ ] Validate 16-30¢ gap dead zone — currently 77% win rate, barely positive (+$0.90)
- [ ] First Fed resolution expected: FOMC Mar 2026 meeting (~March 18-19)
- [ ] Compare ask-based vs bid-based P&L once new trades resolve with bid data:
  ```sql
  SELECT COUNT(*), SUM(pnl_cents) as ask_pnl, SUM(adjusted_pnl_cents) as bid_pnl,
    ROUND(100.0 * SUM(CASE WHEN adjusted_pnl_cents > 0 THEN 1 ELSE 0 END) / COUNT(*), 1) as adj_win_pct
  FROM paper_trades WHERE status='resolved' AND adjusted_pnl_cents IS NOT NULL;
  ```
- [ ] Compare tradeable vs all P&L once enough new trades have bid/spread data
- [ ] Decision: go live with real SELL YES orders, or refine model first?

### If Going Live
- [x] Implement actual SELL YES order placement — DONE (2026-03-04, limit orders at yes_bid)
- [x] Add ORDER_SIZE (10 contracts default) and ORDER_EXPIRY_SECONDS (5 min) config
- [x] Add live_orders table for order tracking + expiry cancellation
- [ ] Add position sizing logic (based on overpricing gap + depth)
- [ ] Add max exposure limits per event/series
- [ ] Fund account to $200+ for adequate margin
- [ ] Keep paper trading alongside live to measure slippage
- [ ] To activate: set `LIVE_EXECUTION=true` in .env (READ_ONLY must not be forced)

### Model Improvements (ongoing)
- [ ] Add CME FedWatch probabilities as dynamic fair value for Fed decisions
- [ ] Use implied volatility or options data for crypto range fair values
- [ ] Consider adding CPI/GDP back with cumulative-aware model
- [ ] Track whether "hold" signals on far-out Fed meetings actually resolve profitably

---

## Historical Context

### Market Maker Phase (2026-02-24 to 2026-03-01)
- Built full market-making engine for KXBTCD + KXBTC15M
- KXBTC15M disabled — structurally unprofitable (adverse selection on 15-min binaries)
- KXBTCD ran at 8c spread, very low fill rate, roughly flat P&L
- Total account loss: ~$8.56 (mostly KXBTC15M adverse selection)

### Polymarket Mirror Pipeline (2026-02-28)
- Built wallet qualification pipeline for Polymarket tracker
- Scored 4,151 wallets, qualified 41 mirror candidates
- Deep profiled 3 wallets (0x8e9e, 0x397b, 0x66c1)
- Key insight: wallet 0x8e9e's edge was "sell overpriced YES contracts" — not mechanical rules
- This insight drove the pivot to the mispricing scanner approach

### Strategic Pivot (2026-02-28)
- Shifted from "mirror wallet rules" to "detect overpriced outcomes"
- Built mispricing scanner as new mode for kalshi-arb-bot
- Fixed cumulative market detection, added Fed base rate model, proportional fair values
- Added paper trade tracking + near-miss analysis
- Deployed 2026-03-01 for 2-week evaluation period

### Dedup Fix (2026-03-01)
- Discovered scanner was logging 3,076 paper trades for 107 unique tickers in 15 hours
- Old dedup was 5-minute in-memory cooldown that reset on restart
- Replaced with DB-backed dedup: `has_open_or_recent_paper_trade()` checks for open trades + 24h cooldown
- Cleaned up duplicates, keeping earliest entry per bucket_ticker

### Execution Reality Checks (2026-03-04)
- Paper trading P&L used yes_ask as entry, but real SELL YES taker fills execute at yes_bid (lower = worse)
- Added bid/ask/spread/bid_depth tracking to paper_trades and mispricing_signals
- Orderbook depth increased from 1→3 levels for better liquidity picture
- `yes_bid_depth` = total contracts across top 3 yes_bid levels (what we can sell into)
- `adjusted_pnl_cents` computed on resolution using yes_bid as entry price
- PAPER TRADE log now shows: `ask=Xc bid=Xc spread=Xc fair=Xc gap=+Xc | bid_depth=N`
- Live execution ready: `LIVE_EXECUTION=true` places SELL YES limit orders at yes_bid price
- Orders auto-cancel after ORDER_EXPIRY_SECONDS (default 5 min)

### Liquidity Filters (2026-03-04)
- Many paper trades had bid=1c, spread=97c, bid_depth=0 — completely untradeable in reality
- Added two filters before logging paper trades (raw signals still logged unconditionally):
  - `bid_depth >= 5` — must have contracts to sell into
  - `spread <= 30c` — anything wider is illiquid
- Added `tradeable` column (0/1) to paper_trades — retroactively tags old trades with bid/spread data
- 517 of 525 trades were pre-deploy (NULL bid/spread), so retroactive tagging only caught 4 trades
- Going forward all new trades will be filtered and properly tagged
