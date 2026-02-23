# Kalshi Arb Bot — Build Plan

## Context
Building a Kalshi arbitrage bot from scratch based on the fix plan PDF. The bot trades KXBTC15M (15-minute BTC binary markets) by detecting when Yes + No best asks sum to less than $1.00, then buying both sides to lock in the spread. The plan already identifies 4 bugs from a previous version — we'll build with those fixes baked in from the start.

## Project Structure
```
C:\Users\ndste\kalshi-arb-bot\
├── .env                  # API credentials (KALSHI_API_KEY, KALSHI_PRIVATE_KEY_PATH, KALSHI_ENV)
├── .env.example          # Template
├── .gitignore
├── requirements.txt      # requests, cryptography, python-dotenv
├── config.py             # All tunable settings
├── auth.py               # RSA-PSS signing + authenticated request helper
├── kalshi_api.py          # API wrapper (markets, orderbook, orders, positions, balance)
├── bot.py                # Core arb bot logic
├── main.py               # Entry point
└── PLAN.md               # Copy of this plan
```

## Key Design Decisions

### 1. No duplicate trades (CRITICAL fix #1)
- Maintain a `traded_tickers: set` — once a ticker is traded, it's blocked until removed
- Clear tickers only when the market closes/settles (not on a timer)
- On each scan loop, skip any ticker already in the set

### 2. Threshold at 95 cents (CRITICAL fix #2)
- `ARB_THRESHOLD = 95` in config — only trade when combined best asks <= 95 cents
- Ensures profit after Kalshi's ~7% taker fee on each leg

### 3. One-sided fill protection (HIGH fix #3)
- After placing both legs, wait 5 seconds and check fill status
- If one leg filled and the other hasn't, cancel the unfilled leg immediately
- If the filled leg leaves us with unhedged exposure, log a warning (manual intervention)
- Additional safety: 8-second hard cancel of any remaining open orders for that ticker

### 4. Real deployed capital tracking (MEDIUM fix #4)
- Query actual positions from `GET /portfolio/positions` to calculate real exposure
- No fake local counter — always use live API data
- Optional max exposure limit in config (default $50,000)

## Verification
1. Set `KALSHI_ENV=demo` in `.env` and run against sandbox
2. Confirm markets are discovered and orderbooks are fetched
3. Verify arb detection logs show correct combined prices
4. Confirm no duplicate trades on the same ticker within a window
5. Test one-sided fill protection by using a very aggressive price on one side
6. Switch to `KALSHI_ENV=prod` for live trading
