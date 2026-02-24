import time
import logging
from dataclasses import dataclass, field

import kalshi_api

logger = logging.getLogger("arb-bot")


# ------------------------------------------------------------------
# Data classes
# ------------------------------------------------------------------

@dataclass
class StrikeLevel:
    ticker: str
    strike: float          # floor_strike from API
    yes_ask: int           # cents (100 - best_no_bid)
    yes_bid: int           # cents (best_yes_bid)
    no_ask: int            # cents (100 - best_yes_bid)
    no_bid: int            # cents (best_no_bid)
    yes_ask_depth: int
    no_ask_depth: int


@dataclass
class LadderSnapshot:
    timestamp: float
    expiry_time: str
    strikes: list[StrikeLevel] = field(default_factory=list)  # sorted ascending by strike


@dataclass
class TradeOpportunity:
    type: str              # "A_monotonicity", "B_probability_gap", "C_hard_arb", "C_soft_arb"
    strikes: list[float]
    tickers: list[str]
    legs: list[dict]       # [{"ticker", "side", "price"}, ...]
    profit_cents: int
    net_profit_cents: float
    confidence: float      # 1.0 for hard arbs, estimated for soft


# ------------------------------------------------------------------
# Build ladder snapshots from markets
# ------------------------------------------------------------------

def build_ladder(markets: list[dict]) -> dict[str, LadderSnapshot]:
    """Group markets by expiry window, fetch orderbooks, return sorted ladders."""
    # Group by expiry
    by_expiry: dict[str, list[dict]] = {}
    for m in markets:
        expiry = m.get("latest_expiration_time", "")
        if not expiry:
            continue
        by_expiry.setdefault(expiry, []).append(m)

    ladders = {}
    now = time.time()

    for expiry, group in by_expiry.items():
        strikes = []
        for m in group:
            ticker = m["ticker"]
            strike = m.get("floor_strike")
            if strike is None:
                continue

            try:
                book = kalshi_api.get_orderbook(ticker, depth=5)
            except Exception:
                logger.debug("Failed to fetch orderbook for %s, skipping", ticker)
                continue

            yes_bids = book.get("yes", [])
            no_bids = book.get("no", [])

            # Skip strikes with empty orderbooks on either side
            if not yes_bids or not no_bids:
                continue

            best_yes_bid = yes_bids[0][0]
            best_no_bid = no_bids[0][0]
            yes_ask = 100 - best_no_bid
            no_ask = 100 - best_yes_bid
            yes_ask_depth = no_bids[0][1]
            no_ask_depth = yes_bids[0][1]

            strikes.append(StrikeLevel(
                ticker=ticker,
                strike=float(strike),
                yes_ask=yes_ask,
                yes_bid=best_yes_bid,
                no_ask=no_ask,
                no_bid=best_no_bid,
                yes_ask_depth=yes_ask_depth,
                no_ask_depth=no_ask_depth,
            ))

        # Sort ascending by strike
        strikes.sort(key=lambda s: s.strike)

        if strikes:
            ladders[expiry] = LadderSnapshot(
                timestamp=now,
                expiry_time=expiry,
                strikes=strikes,
            )

    return ladders


# ------------------------------------------------------------------
# Stale quote filter
# ------------------------------------------------------------------

MIN_QUOTE_DEPTH = 10        # Ignore strikes with depth < 10 on either side
MAX_COMBINED_SPREAD = 110   # Ignore strikes where yes_ask + no_ask > 110


def _is_stale(strike: StrikeLevel) -> bool:
    """True if this strike has phantom/stale quotes."""
    if strike.yes_ask_depth < MIN_QUOTE_DEPTH or strike.no_ask_depth < MIN_QUOTE_DEPTH:
        return True
    if strike.yes_ask + strike.no_ask > MAX_COMBINED_SPREAD:
        return True
    return False


# ------------------------------------------------------------------
# Violation detectors
# ------------------------------------------------------------------

def detect_violations(snapshot: LadderSnapshot, fee_rate: float,
                      prob_threshold: float) -> tuple[list[TradeOpportunity], dict]:
    """Run all detectors. Returns (real_opps, stale_counts).

    stale_counts: dict mapping type string to count of violations filtered
    out because one or both strikes had stale/phantom quotes.
    """
    stale_counts: dict[str, int] = {}

    def _split(raw: list[TradeOpportunity]) -> list[TradeOpportunity]:
        """Keep real opps, count stale ones."""
        real = []
        for opp in raw:
            real.append(opp)
        return real

    opps = []
    real_a, stale_a = detect_type_a(snapshot)
    real_b, stale_b = detect_type_b(snapshot)
    real_c, stale_c = detect_type_c(snapshot, fee_rate, prob_threshold)

    opps.extend(real_a)
    opps.extend(real_b)
    opps.extend(real_c)

    if stale_a:
        stale_counts["A_monotonicity"] = stale_a
    if stale_b:
        stale_counts["B_probability_gap"] = stale_b
    if stale_c:
        stale_counts["C_arb"] = stale_c

    return opps, stale_counts


def detect_type_a(snapshot: LadderSnapshot) -> tuple[list[TradeOpportunity], int]:
    """Monotonicity violations: Yes prices MUST decrease as strikes increase.

    Returns (real_opps, stale_count).
    """
    opps = []
    stale = 0
    strikes = snapshot.strikes
    for i in range(len(strikes) - 1):
        lo = strikes[i]
        hi = strikes[i + 1]
        # If higher strike has a MORE expensive Yes ask, that's a violation
        if hi.yes_ask > lo.yes_ask:
            if _is_stale(lo) or _is_stale(hi):
                stale += 1
                continue
            profit = hi.yes_ask - lo.yes_ask
            opps.append(TradeOpportunity(
                type="A_monotonicity",
                strikes=[lo.strike, hi.strike],
                tickers=[lo.ticker, hi.ticker],
                legs=[
                    {"ticker": lo.ticker, "side": "yes", "price": lo.yes_ask},
                    {"ticker": hi.ticker, "side": "no", "price": hi.no_ask},
                ],
                profit_cents=profit,
                net_profit_cents=float(profit),
                confidence=1.0,
            ))
    return opps, stale


def detect_type_b(snapshot: LadderSnapshot) -> tuple[list[TradeOpportunity], int]:
    """Probability gap: flag adjacent pairs with abnormal yes_ask drop rate.

    Returns (real_opps, stale_count).
    """
    opps = []
    stale = 0
    strikes = snapshot.strikes
    if len(strikes) < 3:
        return opps, 0

    # Compute drop rate per dollar for each adjacent pair
    rates = []
    for i in range(len(strikes) - 1):
        lo = strikes[i]
        hi = strikes[i + 1]
        strike_diff = hi.strike - lo.strike
        if strike_diff <= 0:
            continue
        drop = lo.yes_ask - hi.yes_ask  # expected positive
        rate = drop / strike_diff
        rates.append((i, rate))

    if not rates:
        return opps, 0

    avg_rate = sum(r for _, r in rates) / len(rates)
    if avg_rate == 0:
        return opps, 0

    for idx, rate in rates:
        lo = strikes[idx]
        hi = strikes[idx + 1]
        ratio = rate / avg_rate if avg_rate != 0 else 0

        if ratio > 2.0 or ratio < 0.5:
            if _is_stale(lo) or _is_stale(hi):
                stale += 1
                continue
            opps.append(TradeOpportunity(
                type="B_probability_gap",
                strikes=[lo.strike, hi.strike],
                tickers=[lo.ticker, hi.ticker],
                legs=[
                    {"ticker": lo.ticker, "side": "yes", "price": lo.yes_ask},
                    {"ticker": hi.ticker, "side": "yes", "price": hi.yes_ask},
                ],
                profit_cents=0,
                net_profit_cents=0.0,
                confidence=0.5,
            ))
    return opps, stale


def detect_type_c(snapshot: LadderSnapshot, fee_rate: float,
                  prob_threshold: float) -> tuple[list[TradeOpportunity], int]:
    """Synthetic arbs: buy Yes(low) + No(high) for guaranteed or probable profit.

    Returns (real_opps, stale_count).
    """
    opps = []
    stale = 0
    strikes = snapshot.strikes

    for i in range(len(strikes)):
        lo = strikes[i]
        # Skip if yes_ask too high — can't sum below 100
        if lo.yes_ask > 95:
            continue

        for j in range(i + 1, len(strikes)):
            hi = strikes[j]
            cost = lo.yes_ask + hi.no_ask

            if cost < 100:
                # HARD ARB — guaranteed profit
                if _is_stale(lo) or _is_stale(hi):
                    stale += 1
                    continue
                profit = 100 - cost
                fee_total = fee_rate * 100 * 2  # fee on both legs' payout
                net = profit - fee_total
                opps.append(TradeOpportunity(
                    type="C_hard_arb",
                    strikes=[lo.strike, hi.strike],
                    tickers=[lo.ticker, hi.ticker],
                    legs=[
                        {"ticker": lo.ticker, "side": "yes", "price": lo.yes_ask},
                        {"ticker": hi.ticker, "side": "no", "price": hi.no_ask},
                    ],
                    profit_cents=profit,
                    net_profit_cents=net,
                    confidence=1.0,
                ))
            else:
                # Check for soft arb: cost < 100 + fees and range probability > threshold
                fee_total = fee_rate * 100 * 2
                if cost < 100 + fee_total:
                    # Range probability estimate from implied prices
                    range_prob = lo.yes_ask / 100 - hi.yes_ask / 100
                    if range_prob > prob_threshold:
                        if _is_stale(lo) or _is_stale(hi):
                            stale += 1
                            continue
                        profit = 100 - cost  # negative or zero for soft arb base
                        # Expected profit if BTC lands in range: extra $1 payout
                        expected_bonus = range_prob * 100
                        net = profit + expected_bonus - fee_total
                        if net > 0:
                            opps.append(TradeOpportunity(
                                type="C_soft_arb",
                                strikes=[lo.strike, hi.strike],
                                tickers=[lo.ticker, hi.ticker],
                                legs=[
                                    {"ticker": lo.ticker, "side": "yes", "price": lo.yes_ask},
                                    {"ticker": hi.ticker, "side": "no", "price": hi.no_ask},
                                ],
                                profit_cents=profit,
                                net_profit_cents=net,
                                confidence=range_prob,
                            ))
    return opps, stale


# ------------------------------------------------------------------
# Ranking & logging
# ------------------------------------------------------------------

def rank_opportunities(opps: list[TradeOpportunity]) -> list[TradeOpportunity]:
    """Hard arbs first (by profit desc), then soft arbs (by expected value desc)."""
    hard = [o for o in opps if o.type == "C_hard_arb"]
    soft = [o for o in opps if o.type == "C_soft_arb"]
    other = [o for o in opps if o.type not in ("C_hard_arb", "C_soft_arb")]

    hard.sort(key=lambda o: o.profit_cents, reverse=True)
    soft.sort(key=lambda o: o.net_profit_cents * o.confidence, reverse=True)
    other.sort(key=lambda o: o.profit_cents, reverse=True)

    return hard + soft + other


def log_ladder(snapshot: LadderSnapshot, opportunities: list[TradeOpportunity],
               series_ticker: str = "", stale_counts: dict | None = None):
    """Log compact ladder table and top opportunities."""
    # Extract short expiry label from expiry_time (last 4-5 chars typically HH:MM)
    expiry_short = snapshot.expiry_time[-8:-3] if len(snapshot.expiry_time) >= 8 else snapshot.expiry_time

    header = f"{series_ticker} {expiry_short} | {len(snapshot.strikes)} strikes"
    logger.info(header)
    logger.info(
        "%-12s | %5s | %5s | %5s | %5s | %7s | %7s",
        "strike", "y_ask", "y_bid", "n_ask", "n_bid", "depth_y", "depth_n",
    )
    for s in snapshot.strikes:
        stale_tag = " *" if _is_stale(s) else ""
        logger.info(
            "%-12.2f | %5d | %5d | %5d | %5d | %7d | %7d%s",
            s.strike, s.yes_ask, s.yes_bid, s.no_ask, s.no_bid,
            s.yes_ask_depth, s.no_ask_depth, stale_tag,
        )

    # Violation count by type
    type_counts: dict[str, int] = {}
    for o in opportunities:
        type_counts[o.type] = type_counts.get(o.type, 0) + 1
    parts = [f"{t}={c}" for t, c in sorted(type_counts.items())]

    # Stale violation counts
    if stale_counts:
        stale_parts = [f"stale_{t}={c}" for t, c in sorted(stale_counts.items())]
        parts.extend(stale_parts)

    if parts:
        logger.info("Violations: %s", ", ".join(parts))
    else:
        logger.info("No violations detected")

    # Top 3 opportunities
    for i, opp in enumerate(opportunities[:3]):
        logger.info(
            "  #%d %s strikes=%s profit=%d net=%.1f conf=%.2f",
            i + 1, opp.type, opp.strikes, opp.profit_cents,
            opp.net_profit_cents, opp.confidence,
        )
