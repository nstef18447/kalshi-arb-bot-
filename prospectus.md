# Kalshi Multi-Series Binary Arbitrage Strategy

## Overview

This strategy exploits pricing inefficiencies across Kalshi's multi-strike binary option markets — contracts that resolve based on whether an underlying asset lands above a given strike price at the end of a defined window. When the market misprices the Yes and No sides across different strike pairs such that both can be purchased for less than $1.00 combined, we buy both sides and lock in a guaranteed profit regardless of the outcome, once both legs fill. The bot scans markets across 8 series (5 above/below arb-eligible + 3 range monitoring-only) every few seconds, detecting cross-strike mispricings that are invisible to single-contract traders.

## How Binary Markets Work

Each contract is a yes/no question like: *"Will BTC be $80,000 or above at 6:00 PM?"*

- If you hold **Yes** and the answer is yes, you receive $1.00
- If you hold **No** and the answer is no, you receive $1.00
- One side always pays out — Yes + No = $1.00 guaranteed

Multiple strike prices exist for the same expiry window (e.g., $79,000, $79,500, $80,000...), creating a "ladder" of contracts. As the strike price increases, the Yes price should decrease monotonically — and cross-strike pricing relationships must remain consistent.

## Contract Types on Kalshi

### Above/Below (arb-eligible)
These contracts ask: *"Will the asset be at or above $X?"* Each strike uses `strike_type: greater`. Because the underlying must land somewhere on the number line, buying Yes at a low strike + No at a high strike guarantees at least one payout. Cross-strike arb logic applies.

**Series**: KXBTCD, KXETHD, KXSOLD, KXINXU, KXNASDAQ100U

### Range (monitoring only)
These contracts ask: *"Will the asset land between $X and $Y?"* Each bucket is independent — the price ranges don't overlap. A complete range ladder sums to 400-500c (not ~100c), because the buckets are priced independently. Cross-strike arb logic does **not** apply to range contracts.

**Series**: KXBTC, KXETH, KXSOLE

*Note: The bot initially scanned range contracts (KXBTC/KXETH/KXSOLE) under the assumption they were above/below. This was corrected on Feb 25, 2026 — all Phase 1 data collected before that date is invalid for arb analysis. See "Phase 1 Restart" below.*

## Why Cross-Strike, Not Single-Contract

The naive binary arb — buying Yes and No on the *same* contract for less than $1.00 — is effectively dead. Market makers keep single-contract spreads tight enough that fees consume any edge. On Kalshi, fees follow a parabolic formula peaking at mid-price contracts, making the cost of two 50c legs prohibitive.

The opportunity comes from *cross-strike* pairs: buying Yes at a lower strike and No at a higher strike. Because the underlying must land somewhere, at least one of these pays out $1.00. But the market prices these contracts independently across dozens of simultaneous strikes, and the fragmentation — especially in hourly windows with thin liquidity — creates moments where the combined cost falls below $1.00. This cross-strike mispricing is harder for single-contract market makers to police because it spans different orderbooks.

## Markets Covered

The bot scans 8 series. Crypto above/below series are the primary targets; equity index series are being monitored; range series are tracked for data only.

### Above/Below — Arb-Eligible

| Series | Underlying | Duration | ~Markets | Fee Mult | Poll Rate | Schedule |
|--------|-----------|----------|----------|----------|-----------|----------|
| KXBTCD | Bitcoin | 1h / 25h / 169h | ~165 | 0.0175 | Every cycle | 24/7 |
| KXETHD | Ethereum | 1h / 25h / 169h | ~165 | 0.0175 | Every cycle | 24/7 |
| KXSOLD | Solana | 1h / 25h / 169h | ~200 | 0.0175 | Every cycle | 24/7 |
| KXINXU | S&P 500 | Hourly | ~460 | 0.00875 | Every 6th cycle | US market hours |
| KXNASDAQ100U | Nasdaq-100 | Hourly | ~460 | 0.00875 | Every 6th cycle | US market hours |

### Range — Monitoring Only (no arb logic)

| Series | Underlying | Duration | ~Markets | Fee Mult | Poll Rate | Schedule |
|--------|-----------|----------|----------|----------|-----------|----------|
| KXBTC | Bitcoin | 1h / 25h / 169h | ~165 | 0.0175 | Every 6th cycle | 24/7 |
| KXETH | Ethereum | 1h / 25h / 169h | ~165 | 0.0175 | Every 6th cycle | 24/7 |
| KXSOLE | Solana | 1h / 25h / 169h | ~200 | 0.0175 | Every 6th cycle | 24/7 |

Market durations on Kalshi: hourly (1h), daily (25h), and weekly (169h). There are no 15-minute multi-strike contracts. (KXBTC15M exists but is a single up/down market — useless for cross-strike arb.)

## The Arbitrage

In an efficient market, buying Yes at a lower strike + No at a higher strike should cost $1.00 or more. But these markets are thinly traded and fragmented across dozens of simultaneous strike prices. This creates moments where:

```
Yes Ask (low strike) + No Ask (high strike) < $1.00
```

When this happens, buying both sides guarantees a payout of $1.00 for less than $1.00 invested, once both legs fill. The difference is risk-free profit.

### Opportunity Types

| Type | Description | Risk Level |
|------|-------------|------------|
| **Hard Arb (Type C)** | Combined cost < $1.00 across two strikes. Guaranteed profit once both legs fill. | Risk-free post-fill |
| **Soft Arb (Type C)** | Combined cost slightly above $1.00 but positive expected value if the underlying lands between the strikes. | Low risk, probabilistic |
| **Monotonicity (Type A)** | Higher strike has more expensive Yes than lower strike — structural mispricing. | Informational |
| **Probability Gap (Type B)** | Abnormal price drop rate between adjacent strikes — potential mispricing. | Informational |

### Example (Illustrative arb)

| Component | Value |
|-----------|-------|
| Strike $79,000 — Yes Ask | $0.08 |
| Strike $80,000 — No Ask | $0.78 |
| **Combined cost** | **$0.86** |
| **Guaranteed payout** | **$1.00** |
| **Gross profit** | **$0.14/contract** |

**Fee calculation (worked):**

Kalshi's fee formula is `multiplier * P * (1-P)` per leg, where P is the contract price as a fraction. You pay fees on both legs at purchase.

```
Leg 1 (Yes at 8c):   P = 0.08
  taker: 0.07 * 0.08 * 0.92 * 100 = 0.52c
  maker: 0.0175 * 0.08 * 0.92 * 100 = 0.13c

Leg 2 (No at 78c):   P = 0.78
  taker: 0.07 * 0.78 * 0.22 * 100 = 1.20c
  maker: 0.0175 * 0.78 * 0.22 * 100 = 0.30c

Total fees:   taker = 0.52 + 1.20 = 1.72c     maker = 0.13 + 0.30 = 0.43c
Gross profit: 100 - 86 = 14.00c
Net profit:   taker = 14.00 - 1.72 = 12.28c    maker = 14.00 - 0.43 = 13.57c
```

| Metric | Taker | Maker |
|--------|-------|-------|
| Total fees (both legs) | 1.72c | 0.43c |
| Net profit per contract | 12.28c | 13.57c |
| At 25 contracts | $3.07 | $3.39 |

Note: This example has extreme prices (8c and 78c), where the parabolic fee is low for both order types. The taker/maker difference is more dramatic at mid-range prices near 50c, where fees peak.

## Fee Structure

Kalshi charges fees on both legs using a parabolic formula based on contract price:

```
Fee per leg = multiplier * P * (1 - P) * $1.00
```

Where P is the contract price (0 to 1). This peaks at P = 0.50 (maximum fee) and approaches zero at extreme prices near $0 or $1. The multiplier differs by order type:

| | Crypto (KXBTCD, KXETHD, KXSOLD) | Equity Index (KXINXU, KXNASDAQ100U) |
|---|---|---|
| **Taker multiplier** | 0.07 | 0.035 |
| **Maker multiplier** | 0.0175 | 0.00875 |

Maker fees are 75% lower than taker fees at every price point. Many opportunities that are unprofitable at taker fees become profitable with maker orders. The bot tracks both `estimated_profit` (taker) and `estimated_profit_maker` for every opportunity, and logs a 30-minute summary comparing the two.

### Fee examples at different prices

| Contract price | Taker fee (crypto) | Maker fee (crypto) |
|---------------|-------------------|-------------------|
| 8c | $0.0052 | $0.0013 |
| 25c | $0.0131 | $0.0033 |
| 50c | $0.0175 | $0.0044 |
| 78c | $0.0120 | $0.0030 |

## Entry Criteria

A trade is triggered when all of the following are true:

1. **Combined ask <= $0.95**: The sum of the best Yes ask (low strike) and best No ask (high strike) is 95 cents or less. This threshold accounts for fees on both legs and ensures a net-positive trade.
2. **Contract type is above/below**: Only above/below series are eligible for arb execution. Range contracts are monitoring-only.
3. **Market is open**: The expiry window has not yet expired (based on `close_time`, when trading stops).
4. **Quote quality**: Both strikes must have depth >= 10 contracts on the relevant side, and combined yes_ask + no_ask <= 110 (filters out stale/phantom quotes).
5. **Not already traded**: The bot has not already placed an arb on this specific pair in the current window.
6. **Under exposure limit**: Total deployed capital across all positions is below the configured maximum ($50,000 default).

## Execution

1. Check pre-flight liquidity: both sides need >= 30 contracts at best ask
2. **Leg the illiquid side first** — place the thinner side to minimize directional exposure
3. Wait up to 2 seconds for the first leg to fill
4. If first leg fills, immediately place the second leg
5. If first leg doesn't fill, cancel and abort with zero exposure
6. If second leg doesn't fill within 3 seconds:
   - Cancel the unfilled second leg
   - Sell the filled first leg at best bid immediately (orphan recovery)
   - Log the realized P&L from the exit

### Circuit Breaker
A rolling window tracks the last 20 trade attempts. If the orphan rate exceeds 25%, the bot pauses trading for 10 minutes to avoid compounding losses from adverse market conditions.

## Risk Profile

### What makes this low-risk
- **Outcome-independent**: Profit is locked in at the moment both legs fill, regardless of where the underlying goes
- **Short duration**: Each position resolves within hours — no multi-day exposure
- **Bounded loss**: Maximum loss per trade is the cost of the contracts purchased (no leverage, no margin)
- **Stale quote filtering**: Phantom/stale quotes (low depth or unrealistic pricing) are excluded from opportunity detection

### Residual risks
- **One-sided fills (primary risk)**: If only one leg fills before the other side's price moves, we hold a directional position. The bot mitigates this by selling the orphaned leg immediately and tracking orphan rates via the circuit breaker. This is the most likely source of loss.
- **Execution/latency**: Arb opportunities are fleeting. Running on a low-latency cloud server reduces this risk, but opportunities may vanish between detection and order placement.
- **Fee changes**: The strategy's profitability depends on Kalshi's current fee structure. An increase in fees or change to the parabolic formula would narrow the profitable threshold.
- **Liquidity**: Very thin orderbooks may result in partial fills or wide spreads that reduce effective profitability.
- **API/platform risk**: Downtime, rate limits, or API changes on Kalshi's side could disrupt execution.
- **Market hours**: Equity index opportunities only exist during US trading hours; crypto series run 24/7.

## Competition and Sustainability

### Why this opportunity exists
- **Market fragmentation**: Each expiry window has 20-75+ independent strike contracts with separate orderbooks. Market makers price each contract individually, and cross-strike consistency isn't enforced by the exchange.
- **Rapid turnover**: Hourly, daily, and weekly contract windows mean new ladders are constantly being populated. Market makers can't maintain perfect pricing across all strikes at all times.
- **Retail-heavy platform**: Kalshi's user base skews retail, meaning less sophisticated pricing and wider spreads compared to institutional derivatives markets.
- **Fee structure**: The parabolic fee formula creates non-obvious cost curves that retail traders may not fully account for when pricing their orders.

### How long will it last?
This is unknown. The opportunity will likely shrink as:
- More automated participants enter the market
- Kalshi's market maker programs mature and tighten cross-strike consistency
- Trading volume increases and orderbooks deepen

The 30-minute maker fee summary tracks trend data specifically to detect opportunity compression over time. If profitable hard arbs per hour decreases consistently, that signals the window is closing.

### What happens when it shrinks
The strategy degrades gracefully. As spreads narrow:
1. Hard arbs become less frequent and smaller
2. Maker fees (75% cheaper) extend the profitable range longer than taker fees
3. Below a certain threshold, the bot simply stops finding opportunities and sits idle — it doesn't lose money, it just stops making it

## Preliminary Observations

### Phase 1 Restart (Feb 25, 2026)

The initial Phase 1 data (Feb 23-24, 2026) was collected on **range contracts** (KXBTC, KXETH, KXSOLE), where cross-strike arb logic does not apply. That data is invalid for validating this strategy. Key finding: complete range ladders sum to 400-500c, confirming they are independently-priced buckets with no arb potential.

Phase 1 was restarted on Feb 25, 2026 with the correct **above/below** series (KXBTCD, KXETHD, KXSOLD, KXINXU, KXNASDAQ100U). Fresh data is now accumulating.

### Initial above/below observations (limited data)

Early scanning of KXBTCD hourly windows shows:
- 6+ strikes per hourly expiry with monotonically decreasing Yes prices — correct structure for cross-strike arb
- Combined costs observed at 105-109c during thin overnight hours (no arbs)
- Deeper data across active trading hours is needed before drawing conclusions

Full preliminary analysis will be updated after 48+ hours of above/below data collection.

## Expected Performance

Performance projections will be populated from Phase 1 validation data after 48+ hours of multi-series above/below scanning. Estimated completion: Feb 27, 2026.

Key metrics to be reported:
- Hard arbs per hour by series
- Percentage profitable at taker vs maker fees
- Average and median gross spread
- Average depth available
- Opportunity persistence (how long arbs last before closing)
- Arb stability across consecutive scans
- Estimated daily/monthly P&L at various position sizes

## Capital Requirements

- **Minimum**: ~$50 to run a handful of trades at 25 contracts per leg
- **Recommended**: $500-$2,000 for comfortable operation across multiple simultaneous opportunities
- **Maximum deployed** (configurable): $50,000 hard cap enforced by the bot

Capital is recycled rapidly since each position resolves within hours.

## Infrastructure

- Runs as a Python process on a cloud server (DigitalOcean droplet, 2GB RAM)
- Scans all open markets across 8 series every cycle (above/below every cycle, range every 6th)
- Authenticates via Kalshi's RSA-PSS signed API (v2)
- Production API: `api.elections.kalshi.com`
- SQLite database (WAL mode) for analytics: scans, ladder snapshots, opportunities, trades, arb stability
- Streamlit dashboard for real-time monitoring (reads same DB)
- Currently running in READ_ONLY mode (Phase 1 data collection)
- Stale quote detection and separate logging for data quality
- Orderbook stability tracking across consecutive scans
- Scan cycle timing instrumentation for performance monitoring

## Roadmap

### Phase 1: Validation (current — restarted Feb 25)
Scan-only mode collecting data across all above/below series. No capital at risk. Goal: confirm opportunity frequency, spread sizes, depth, and persistence across multiple 24-hour cycles and market conditions on the correct contract type.

**Exit criteria**: 48+ hours of above/below data showing consistent maker-profitable hard arbs with median depth > 100 contracts.

### Phase 2: Paper trading
Simulate execution against live orderbook data. Log hypothetical fills, orphan rates, and P&L without placing real orders. Validates that opportunities persist long enough to execute sequentially (illiquid leg first, then liquid leg).

**Exit criteria**: Simulated orphan rate < 25%, simulated P&L positive over 24+ hours.

### Phase 3: Small live ($50-500)
Enable live trading with minimal capital. `MAX_CONTRACTS=5`, `MAX_EXPOSURE=500_00`. Focus on execution quality — fill rates, slippage, orphan recovery — not P&L optimization.

**Exit criteria**: 50+ real trades with orphan rate < 15%, no circuit breaker trips, positive realized P&L.

### Phase 4: Scale ($500-5,000)
Increase position sizes gradually. `MAX_CONTRACTS=15-25`, `MAX_EXPOSURE=5000_00`. Monitor for market impact — do our orders move the book? Do arbs close faster as we trade?

**Exit criteria**: Stable positive P&L at scale, no evidence of adverse selection or market impact.

### Phase 5: Full operation
Target capital deployment with continuous monitoring. Maker order strategy for lower fees. Dashboard alerts for opportunity compression or anomalous orphan rates.

Each phase requires explicit sign-off before advancing. The bot can be returned to READ_ONLY mode at any phase by setting a single environment variable.

## Key Assumptions

1. Kalshi's above/below multi-strike series continue to operate with current rules and fee structures
2. Binary payout remains fixed at $1.00
3. Orderbook data from the API reflects live, executable prices
4. API rate limits are sufficient for the polling frequency used
5. Fee formula remains `multiplier * P * (1-P)` for both taker and maker orders
6. Cross-strike arb opportunities persist at actionable frequency (to be validated in Phase 1)
