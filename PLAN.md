# Kalshi Arb Bot — Build Plan

## Context
Arbitrage bot for Kalshi's KXBTC15M (15-minute BTC binary) markets. Detects when Yes + No best asks sum to less than $1.00, buys both sides to lock in the spread. Built with 4 original bug fixes baked in, then refactored with 5 execution improvements to minimize one-sided fill risk.

## Project Structure
```
C:\Users\ndste\kalshi-arb-bot\
├── .env                  # API credentials (KALSHI_API_KEY, KALSHI_PRIVATE_KEY_PATH, KALSHI_ENV)
├── .env.example          # Template
├── .gitignore
├── requirements.txt      # requests, cryptography, python-dotenv
├── config.py             # All tunable settings
├── auth.py               # RSA-PSS signing + authenticated request helper
├── kalshi_api.py         # API wrapper (markets, orderbook, orders, positions, balance, sell)
├── bot.py                # Core arb bot logic
├── main.py               # Entry point
├── PROSPECTUS.md         # Strategy prospectus
└── PLAN.md               # This file
```

## Original Fixes (v1)

### 1. No duplicate trades (CRITICAL)
- `traded_tickers: set` blocks re-trading a ticker until it settles

### 2. Threshold at 95 cents (CRITICAL)
- `ARB_THRESHOLD = 95` ensures profit after ~7% taker fee on each leg

### 3. One-sided fill protection (HIGH)
- Replaced by v2 sequential execution (see below)

### 4. Real deployed capital tracking (MEDIUM)
- Live positions from API, no local counter, $50k max exposure cap

## Execution Refactor (v2) — Current Implementation

### 1. Pre-flight liquidity check
- Both sides must have >= `MIN_DEPTH` (30) contracts at best ask before placing any orders
- Prevents entering markets too thin to reliably fill both legs

### 2. Leg the illiquid side first
- Compare depth on both sides, place the thinner side first
- Wait up to `FIRST_LEG_TIMEOUT` (2s) for fill
- If first leg doesn't fill → cancel and abort with zero exposure
- If it fills → immediately place second leg

### 3. Orphan fill recovery
- If second leg fails to fill within `SECOND_LEG_TIMEOUT` (3s):
  - Cancel unfilled second leg
  - Sell filled first leg at best bid immediately
  - Log ticker, side, fill price, exit price, realized P&L
  - Never hold orphaned positions

### 4. Circuit breaker
- Rolling window of last `WINDOW_SIZE` (20) attempts
- If orphan rate > `MAX_ORPHAN_RATE` (25%) → pause for `COOLDOWN_MINUTES` (10)
- Prevents bleeding money in fast/thin conditions

### 5. Structured logging
- Every attempt logs: timestamp, ticker, prices, depths, which side first, fill status, orphan rate

## Config Variables
```python
ARB_THRESHOLD = 95          # Max combined price in cents
MAX_CONTRACTS = 25          # Contracts per leg
POLL_INTERVAL = 5           # Seconds between scans
SERIES = ["KXBTC15M"]      # Series to monitor
MAX_EXPOSURE = 50000_00     # Max capital in cents ($50k)
MIN_DEPTH = 30              # Min contracts at best ask
FIRST_LEG_TIMEOUT = 2       # Seconds for illiquid leg fill
SECOND_LEG_TIMEOUT = 3      # Seconds for second leg fill
WINDOW_SIZE = 20            # Rolling window for orphan rate
MAX_ORPHAN_RATE = 0.25      # Circuit breaker threshold
COOLDOWN_MINUTES = 10       # Pause duration on trip
```

## Deployment
- Hosted on Digital Ocean droplet
- Repo: https://github.com/nstef18447/kalshi-arb-bot-.git
- Run with `screen -S arb && python main.py`
- Start with `KALSHI_ENV=demo`, switch to `prod` when validated

## Next Steps
- [ ] Set up Kalshi API key and PEM file
- [ ] Deploy to DO droplet and test against demo API
- [ ] Validate arb detection, depth checks, and orphan recovery in sandbox
- [ ] Switch to prod
