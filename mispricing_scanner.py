"""Mispricing scanner for multi-outcome Kalshi markets.

Inspired by wallet 0x8e9e's strategy: find outcomes priced above fair value
and flag them as SELL YES signals. Operates in READ_ONLY mode — logs signals
to SQLite for review, never places orders.

Scans multi-outcome markets (Fed rate ranges, CPI buckets, BTC/ETH price ranges),
checks if YES prices sum to significantly more than $1.00 (the overpricing spread),
and compares each bucket to fair-value estimates based on historical base rates
and consensus forecasts.
"""

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import config
import db_logger
import kalshi_api

logger = logging.getLogger("mispricing")

# ── Multi-outcome series to scan ─────────────────────────────────────────
# These are range/bucket contracts where outcomes are mutually exclusive
# and YES prices across all buckets should theoretically sum to ~$1.00.
MULTI_OUTCOME_SERIES = {
    # Crypto price ranges (daily) — TRUE multi-outcome (range buckets)
    "KXBTC":   {"category": "crypto",    "poll_every": 30, "label": "BTC Daily Range"},
    "KXETH":   {"category": "crypto",    "poll_every": 30, "label": "ETH Daily Range"},
    "KXSOLE":  {"category": "crypto",    "poll_every": 30, "label": "SOL Daily Range"},
    # Fed / economics — TRUE multi-outcome (exclusive outcomes)
    "KXFEDDECISION":  {"category": "economics", "poll_every": 120, "label": "Fed Rate Decision"},
    "KXRATECUTCOUNT": {"category": "economics", "poll_every": 300, "label": "Rate Cut Count 2026"},
    # Political multi-outcome
    "KXBALANCEPOWERCOMBO": {"category": "politics", "poll_every": 300, "label": "Congress Balance of Power"},
}
# NOTE: KXCPIYOY, KXCPICOREYOY, KXGDP are EXCLUDED — they are cumulative
# threshold markets ("above X%"), not mutually exclusive ranges. Their YES
# prices naturally sum to 400-800c, not ~100c. The $1.00-sum property does
# not apply to them.

# ── Overpricing thresholds ───────────────────────────────────────────────
OVERPRICING_THRESHOLD_CENTS = config.MISPRICING_THRESHOLD   # Flag bucket if Kalshi price > fair value by this amount
MIN_TOTAL_EXCESS_CENTS = config.MISPRICING_MIN_EXCESS       # Only scan events where total YES sum > 100 + this
MIN_BUCKETS = 3                    # Need at least 3 outcomes to be a valid multi-outcome
MIN_BID_DEPTH = 5                  # Don't paper trade if < 5 contracts to sell into
MAX_SPREAD = 30                    # Don't paper trade if bid-ask spread > 30c

# ── Fair value models ────────────────────────────────────────────────────
# Static base rates derived from historical data. These are starting points —
# the scanner flags deviations from these, not trade recommendations.

# Fed rate decisions: historical frequencies of outcomes at FOMC meetings
# Source: 2015-2025 FOMC decisions
FED_DECISION_BASE_RATES = {
    "cut_50":   0.05,   # 50bp cut
    "cut_25":   0.25,   # 25bp cut
    "hold":     0.55,   # No change
    "hike_25":  0.12,   # 25bp hike
    "hike_50":  0.03,   # 50bp hike
}

# CPI YoY: distribution of monthly CPI readings (2020-2025)
# Bucket centers with approximate historical frequency
CPI_YOY_BASE_RATES = {
    "below_2.0":   0.08,
    "2.0_to_2.5":  0.22,
    "2.5_to_3.0":  0.28,
    "3.0_to_3.5":  0.20,
    "3.5_to_4.0":  0.12,
    "above_4.0":   0.10,
}


@dataclass
class BucketSnapshot:
    """A single bucket/outcome within a multi-outcome event."""
    ticker: str
    subtitle: str           # e.g., "Above $95,000" or "25bp cut"
    yes_ask: int            # cents (best ask for YES)
    yes_bid: int            # cents (best bid for YES)
    no_ask: int
    no_bid: int
    yes_depth: int          # contracts at best ask
    no_depth: int
    yes_bid_depth: int = 0  # total contracts across top yes_bid levels (sellable liquidity)
    strike: float | None = None    # numeric value if extractable


@dataclass
class EventSnapshot:
    """All buckets for a single multi-outcome event."""
    event_ticker: str
    series_ticker: str
    title: str
    category: str
    buckets: list[BucketSnapshot] = field(default_factory=list)
    total_yes_ask: int = 0     # sum of all yes_ask prices (should be ~100c)
    excess_cents: int = 0      # total_yes_ask - 100 (the "vig" or overpricing)
    timestamp: float = 0.0


@dataclass
class MispricingSignal:
    """A flagged overpriced bucket."""
    event_ticker: str
    series_ticker: str
    event_title: str
    bucket_ticker: str
    bucket_label: str
    category: str
    current_price: int           # cents (yes_ask)
    fair_value_est: int          # cents (estimated fair value)
    overpricing_gap: int         # current_price - fair_value_est
    total_event_excess: int      # excess across all buckets in this event
    yes_depth: int
    yes_bid: int = 0             # best bid for YES at signal time
    yes_ask: int = 0             # best ask for YES (same as current_price)
    spread: int = 0              # yes_ask - yes_bid
    bid_depth: int = 0           # contracts available across top yes_bid levels
    timestamp: float = 0.0


def _extract_strike(subtitle: str) -> float | None:
    """Try to extract a numeric strike/threshold from a market subtitle."""
    # Patterns: "Above $95,000", "Below 2.5%", "Between 3.0% and 3.5%"
    m = re.search(r"[\$]?([\d,]+\.?\d*)", subtitle.replace(",", ""))
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass
    return None


def _estimate_fair_value_uniform(n_buckets: int) -> dict[int, int]:
    """Simplest model: uniform distribution across buckets.

    Returns {bucket_index: fair_value_cents}.
    """
    fair = round(100 / n_buckets)
    return {i: fair for i in range(n_buckets)}


def _estimate_fair_value_center_weighted(n_buckets: int) -> dict[int, int]:
    """Center-weighted model: middle buckets get more probability.

    Useful for price ranges where the current price is roughly centered.
    """
    if n_buckets <= 2:
        return _estimate_fair_value_uniform(n_buckets)

    weights = []
    for i in range(n_buckets):
        # Triangle distribution peaking at center
        center = (n_buckets - 1) / 2
        dist = abs(i - center) / center
        weights.append(max(1 - dist, 0.1))

    total_w = sum(weights)
    return {i: round(100 * w / total_w) for i, w in enumerate(weights)}


def _match_fed_bucket(subtitle: str) -> str | None:
    """Map a Fed decision market subtitle to a base rate key."""
    sub = subtitle.lower().strip()
    if "50" in sub and "cut" in sub:
        return "cut_50"
    if "25" in sub and "cut" in sub:
        return "cut_25"
    if "0 bps" in sub or "no change" in sub or "hold" in sub or "hike 0" in sub:
        return "hold"
    if "25" in sub and "hike" in sub:
        return "hike_25"
    if "50" in sub and "hike" in sub:
        return "hike_50"
    return None


def _estimate_fair_values_fed(event: EventSnapshot) -> dict[int, int] | None:
    """Use historical base rates for Fed decision markets.

    Returns None if we can't match all buckets or if the event is too
    near-term (< 90 days) — near-term meetings have real-time consensus
    that dominates historical base rates.
    """
    # Skip near-term events where market consensus is more informative
    # than historical base rates. Parse month/year from event title.
    title = event.title.lower()
    month_map = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
                 "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}
    event_month = event_year = None
    for abbr, num in month_map.items():
        if abbr in title:
            event_month = num
            break
    year_match = re.search(r"20(\d{2})", title)
    if year_match:
        event_year = 2000 + int(year_match.group(1))

    if event_month and event_year:
        now = datetime.now(timezone.utc)
        # Approximate days until event
        event_approx = datetime(event_year, event_month, 15, tzinfo=timezone.utc)
        days_out = (event_approx - now).days
        if days_out < 90:
            return None  # Too close — use proportional model instead

    fair_values = {}
    for i, bucket in enumerate(event.buckets):
        key = _match_fed_bucket(bucket.subtitle)
        if key and key in FED_DECISION_BASE_RATES:
            fair_values[i] = round(FED_DECISION_BASE_RATES[key] * 100)
        else:
            return None  # Can't match all buckets, fall back to generic

    # Normalize to 100
    total = sum(fair_values.values())
    if total != 100 and total > 0:
        diff = 100 - total
        max_idx = max(fair_values, key=fair_values.get)
        fair_values[max_idx] += diff

    return fair_values


def _estimate_fair_values(event: EventSnapshot) -> dict[int, int]:
    """Estimate fair value for each bucket in an event.

    Returns {bucket_index: fair_value_cents}.

    Uses series-specific models where available, falls back to
    center-weighted distribution.
    """
    n = len(event.buckets)
    if n == 0:
        return {}

    # Fed decision: use historical base rates
    if event.series_ticker == "KXFEDDECISION":
        fed_fv = _estimate_fair_values_fed(event)
        if fed_fv is not None:
            return fed_fv

    # Default: market-implied proportional fair values.
    # Scale each bucket's price down so the total sums to 100c.
    # The excess is distributed as "vig" across all buckets in proportion
    # to their price. A bucket carrying MORE than its proportional share
    # of the vig is a cross-bucket anomaly signal.
    total_yes = event.total_yes_ask
    if total_yes > 100:
        fair_values = {}
        for i, bucket in enumerate(event.buckets):
            fair_values[i] = max(1, round(bucket.yes_ask * 100 / total_yes))
        # Normalize to exactly 100 — only adjust buckets with room to spare
        total_fv = sum(fair_values.values())
        if total_fv != 100 and total_fv > 0:
            diff = 100 - total_fv
            # Sort by value descending so we adjust the largest buckets
            sorted_idxs = sorted(fair_values, key=fair_values.get, reverse=True)
            step = 1 if diff > 0 else -1
            for _ in range(abs(diff)):
                for idx in sorted_idxs:
                    if step < 0 and fair_values[idx] <= 1:
                        continue  # Don't go below 1
                    fair_values[idx] += step
                    break
        return fair_values

    # Fallback for markets priced at or below 100c total
    fair_values = _estimate_fair_value_center_weighted(n)
    total = sum(fair_values.values())
    if total != 100 and total > 0:
        diff = 100 - total
        max_idx = max(fair_values, key=fair_values.get)
        fair_values[max_idx] += diff

    return fair_values


def _fetch_event_snapshot(event: dict, series_ticker: str, series_cfg: dict) -> EventSnapshot | None:
    """Fetch orderbook data for all markets in an event and build a snapshot."""
    event_ticker = event.get("event_ticker", "")
    title = event.get("title", event_ticker)
    category = series_cfg.get("category", "unknown")

    markets = kalshi_api.get_markets_for_event(event_ticker, status="open")
    if len(markets) < MIN_BUCKETS:
        return None

    buckets = []
    for m in markets:
        ticker = m.get("ticker", "")
        subtitle = m.get("subtitle", m.get("title", ticker))
        if not ticker:
            continue

        try:
            book = kalshi_api.get_orderbook(ticker, depth=3)
        except Exception:
            logger.debug("Failed to fetch orderbook for %s", ticker)
            continue

        yes_bids = book.get("yes", [])
        no_bids = book.get("no", [])

        yes_ask = 100 - no_bids[0][0] if no_bids else 99
        yes_bid = yes_bids[0][0] if yes_bids else 1
        no_ask = 100 - yes_bids[0][0] if yes_bids else 99
        no_bid = no_bids[0][0] if no_bids else 1
        yes_depth = no_bids[0][1] if no_bids else 0
        no_depth = yes_bids[0][1] if yes_bids else 0
        # Total contracts across all yes_bid levels (liquidity we can sell into)
        yes_bid_depth = sum(level[1] for level in yes_bids) if yes_bids else 0

        buckets.append(BucketSnapshot(
            ticker=ticker,
            subtitle=subtitle,
            yes_ask=yes_ask,
            yes_bid=yes_bid,
            no_ask=no_ask,
            no_bid=no_bid,
            yes_depth=yes_depth,
            no_depth=no_depth,
            yes_bid_depth=yes_bid_depth,
            strike=_extract_strike(subtitle),
        ))

    if len(buckets) < MIN_BUCKETS:
        return None

    # Sort by strike if available, otherwise keep original order
    if all(b.strike is not None for b in buckets):
        buckets.sort(key=lambda b: b.strike)

    total_yes = sum(b.yes_ask for b in buckets)
    excess = total_yes - 100

    # Safety check: detect cumulative/threshold markets that slipped through.
    # Cumulative "above X" markets have YES prices that decrease monotonically
    # and sum to far more than 100c. True multi-outcome should sum to ~100-115c.
    if total_yes > 200 and len(buckets) >= 4:
        # Check if prices are roughly monotonically decreasing (cumulative signal)
        decreasing = sum(
            1 for i in range(len(buckets) - 1)
            if buckets[i].yes_ask >= buckets[i + 1].yes_ask
        )
        if decreasing >= len(buckets) * 0.7:
            logger.debug(
                "Skipping %s — looks cumulative (YES sum=%dc, %d/%d decreasing)",
                event_ticker, total_yes, decreasing, len(buckets) - 1,
            )
            return None

    return EventSnapshot(
        event_ticker=event_ticker,
        series_ticker=series_ticker,
        title=title,
        category=category,
        buckets=buckets,
        total_yes_ask=total_yes,
        excess_cents=excess,
        timestamp=time.time(),
    )


def _get_signal_type(event: EventSnapshot) -> str:
    """Classify the fair-value model used for this event."""
    if event.series_ticker == "KXFEDDECISION":
        # Check if Fed base rates were actually used (>90 days out)
        title = event.title.lower()
        month_map = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
                     "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}
        event_month = event_year = None
        for abbr, num in month_map.items():
            if abbr in title:
                event_month = num
                break
        year_match = re.search(r"20(\d{2})", title)
        if year_match:
            event_year = 2000 + int(year_match.group(1))
        if event_month and event_year:
            now = datetime.now(timezone.utc)
            event_approx = datetime(event_year, event_month, 15, tzinfo=timezone.utc)
            if (event_approx - now).days >= 90:
                return "fed_base_rate"
    if event.category == "crypto":
        return "crypto_range"
    return "proportional"


def _detect_mispricings(event: EventSnapshot) -> tuple[list[MispricingSignal], list[dict]]:
    """Compare each bucket to fair-value estimate and flag overpriced ones.

    Returns (signals, near_misses) where near_misses are buckets within 50-99%
    of the threshold.
    """
    fair_values = _estimate_fair_values(event)
    signal_type = _get_signal_type(event)

    has_external_model = event.series_ticker in ("KXFEDDECISION",)
    threshold = OVERPRICING_THRESHOLD_CENTS if has_external_model else max(5, OVERPRICING_THRESHOLD_CENTS // 2)

    # Near-miss zone: gap is >= 50% of threshold but < threshold
    near_miss_floor = max(2, threshold // 2)

    signals = []
    near_misses = []

    for i, bucket in enumerate(event.buckets):
        fv = fair_values.get(i, round(100 / len(event.buckets)))
        gap = bucket.yes_ask - fv

        if gap >= threshold:
            signals.append(MispricingSignal(
                event_ticker=event.event_ticker,
                series_ticker=event.series_ticker,
                event_title=event.title,
                bucket_ticker=bucket.ticker,
                bucket_label=bucket.subtitle,
                category=event.category,
                current_price=bucket.yes_ask,
                fair_value_est=fv,
                overpricing_gap=gap,
                total_event_excess=event.excess_cents,
                yes_depth=bucket.yes_depth,
                yes_bid=bucket.yes_bid,
                yes_ask=bucket.yes_ask,
                spread=bucket.yes_ask - bucket.yes_bid,
                bid_depth=bucket.yes_bid_depth,
                timestamp=event.timestamp,
            ))
        elif gap >= near_miss_floor:
            near_misses.append({
                "event_ticker": event.event_ticker,
                "series_ticker": event.series_ticker,
                "bucket_ticker": bucket.ticker,
                "bucket_label": bucket.subtitle,
                "category": event.category,
                "signal_type": signal_type,
                "yes_price": bucket.yes_ask,
                "fair_value_est": fv,
                "gap": gap,
                "threshold_used": threshold,
            })

    return signals, near_misses


class MispricingScanner:
    """Main scanner loop for multi-outcome mispricing detection."""

    def __init__(self):
        self.running = False
        self.scan_count = 0
        self.last_poll: dict[str, float] = {}  # series -> last poll time
        self.last_resolution_check: float = 0  # monotonic time
        self.stats = {
            "scans": 0,
            "events_checked": 0,
            "signals_found": 0,
            "overpriced_events": 0,
            "paper_trades": 0,
            "resolved_trades": 0,
        }
        # Paper trade dedup: 24h cooldown, checked via DB
        self._paper_trade_cooldown = 86400  # 24 hours
        # Track recent near-misses (in-memory, 10 min cooldown is fine)
        self._recent_near_misses: dict[str, float] = {}
        self._near_miss_cooldown = 600  # 10 min between same-bucket near-miss logs

    def start(self):
        self.running = True
        logger.info("Mispricing scanner started — monitoring %d series", len(MULTI_OUTCOME_SERIES))
        while self.running:
            try:
                self._scan_cycle()
            except KeyboardInterrupt:
                break
            except Exception:
                logger.exception("Error in mispricing scan cycle")
            time.sleep(5)

    def stop(self):
        self.running = False
        logger.info("Mispricing scanner stopped — stats: %s", self.stats)

    def _check_resolutions(self):
        """Check if any open paper trades have resolved."""
        open_trades = db_logger.get_open_paper_trades()
        if not open_trades:
            return

        # Group by event_ticker to batch API calls
        by_event: dict[str, list[dict]] = {}
        for t in open_trades:
            by_event.setdefault(t["event_ticker"], []).append(t)

        for event_ticker, trades in by_event.items():
            try:
                # Check if markets have settled by looking for them with settled status
                settled_markets = kalshi_api.get_markets_for_event(event_ticker, status="settled")
            except Exception:
                logger.debug("Failed to check resolution for %s", event_ticker)
                continue

            if not settled_markets:
                # Also check if event itself still has open markets
                try:
                    open_markets = kalshi_api.get_markets_for_event(event_ticker, status="open")
                except Exception:
                    continue
                if open_markets:
                    continue  # Event still active
                # No open AND no settled = try fetching individual market details
                for trade in trades:
                    try:
                        market = kalshi_api.get_market(trade["bucket_ticker"])
                        result = market.get("result", "")
                        status = market.get("status", "")
                        if status in ("settled", "finalized"):
                            # result is "yes" or "no"
                            self._resolve_trade(trade, result)
                    except Exception:
                        logger.debug("Failed to fetch market %s for resolution", trade["bucket_ticker"])
                continue

            # Build result map from settled markets
            result_map = {}
            for m in settled_markets:
                ticker = m.get("ticker", "")
                result = m.get("result", "")
                if ticker and result:
                    result_map[ticker] = result

            for trade in trades:
                result = result_map.get(trade["bucket_ticker"])
                if result:
                    self._resolve_trade(trade, result)

    def _resolve_trade(self, trade: dict, result: str):
        """Resolve a single paper trade with the market outcome."""
        entry_price = trade["entry_price"]
        # We hypothetically SOLD YES at entry_price (yes_ask).
        # If result="yes": the YES contract paid out 100c, we lose (100 - entry_price)
        # If result="no": the YES contract is worthless, we keep entry_price
        if result == "yes":
            pnl = -(100 - entry_price)  # We sold at entry_price, it settled at 100
        else:
            pnl = entry_price  # We sold at entry_price, it settled at 0

        # Adjusted P&L: what if we sold at yes_bid (realistic taker fill)?
        yes_bid = trade.get("yes_bid")
        adjusted_pnl = None
        if yes_bid is not None:
            if result == "yes":
                adjusted_pnl = -(100 - yes_bid)
            else:
                adjusted_pnl = yes_bid

        db_logger.resolve_paper_trade(trade["id"], 100 if result == "yes" else 0, pnl, adjusted_pnl)
        self.stats["resolved_trades"] += 1
        adj_str = f" adj={adjusted_pnl:+d}c" if adjusted_pnl is not None else ""
        logger.info(
            "RESOLVED: %s | %s | sold@%dc → %s | P&L=%+dc%s",
            trade["event_ticker"], trade["bucket_ticker"],
            entry_price, result.upper(), pnl, adj_str,
        )

    def _execute_live_order(self, signal):
        """Place a SELL YES limit order at yes_bid price."""
        if signal.yes_bid <= 0:
            logger.debug("Skipping live order for %s — yes_bid=%d", signal.bucket_ticker, signal.yes_bid)
            return
        try:
            result = kalshi_api.create_sell_order(
                ticker=signal.bucket_ticker,
                side="yes",
                price_cents=signal.yes_bid,
                count=config.ORDER_SIZE,
            )
            order_id = result.get("order_id", result.get("order", {}).get("order_id", ""))
            expires_at = time.time() + config.ORDER_EXPIRY_SECONDS
            db_logger.log_live_order(
                paper_trade_id=None,
                order_id=order_id,
                bucket_ticker=signal.bucket_ticker,
                price_cents=signal.yes_bid,
                count=config.ORDER_SIZE,
                expires_at=expires_at,
            )
            logger.warning(
                "LIVE ORDER: SELL YES %s @%dc x%d | order_id=%s",
                signal.bucket_ticker, signal.yes_bid, config.ORDER_SIZE, order_id,
            )
        except Exception:
            logger.exception("Failed to place live order for %s", signal.bucket_ticker)

    def _check_order_expiry(self):
        """Cancel orders older than ORDER_EXPIRY_SECONDS."""
        open_orders = db_logger.get_open_live_orders()
        now = time.time()
        for order in open_orders:
            if now >= order["expires_at"]:
                try:
                    kalshi_api.cancel_order(order["order_id"])
                    db_logger.update_live_order(order["id"], status="cancelled", cancelled_at=now)
                    logger.info("CANCELLED expired order %s for %s", order["order_id"], order["bucket_ticker"])
                except Exception:
                    logger.debug("Failed to cancel order %s", order["order_id"])

    def _scan_cycle(self):
        self.scan_count += 1
        self.stats["scans"] += 1
        now = time.monotonic()

        # Check paper trade resolutions every 5 minutes
        if now - self.last_resolution_check >= 300:
            self.last_resolution_check = now
            try:
                self._check_resolutions()
            except Exception:
                logger.exception("Error checking paper trade resolutions")

        # Check live order expiry every cycle
        if config.LIVE_EXECUTION and not config.READ_ONLY:
            try:
                self._check_order_expiry()
            except Exception:
                logger.debug("Error checking order expiry")

        for series_ticker, series_cfg in MULTI_OUTCOME_SERIES.items():
            poll_every = series_cfg.get("poll_every", 60)
            last = self.last_poll.get(series_ticker, 0)
            if now - last < poll_every:
                continue
            self.last_poll[series_ticker] = now

            try:
                events = kalshi_api.get_events(series_ticker, status="open")
            except Exception:
                logger.debug("Failed to fetch events for %s", series_ticker)
                continue

            if not events:
                continue

            for event in events:
                snapshot = _fetch_event_snapshot(event, series_ticker, series_cfg)
                if not snapshot:
                    continue

                self.stats["events_checked"] += 1

                # Log event-level summary
                if snapshot.excess_cents >= MIN_TOTAL_EXCESS_CENTS:
                    self.stats["overpriced_events"] += 1
                    logger.info(
                        "[%s] %s — %d buckets, YES sum=%dc (excess=%dc)",
                        series_ticker, snapshot.title[:60],
                        len(snapshot.buckets), snapshot.total_yes_ask, snapshot.excess_cents,
                    )

                    # Detect mispriced buckets + near misses
                    signal_type = _get_signal_type(snapshot)
                    signals, near_misses = _detect_mispricings(snapshot)

                    for sig in signals:
                        self.stats["signals_found"] += 1

                        # Always log the raw signal to mispricing_signals
                        db_logger.log_mispricing_signal(
                            event_ticker=sig.event_ticker,
                            series_ticker=sig.series_ticker,
                            event_title=sig.event_title,
                            bucket_ticker=sig.bucket_ticker,
                            bucket_label=sig.bucket_label,
                            category=sig.category,
                            current_price=sig.current_price,
                            fair_value_est=sig.fair_value_est,
                            overpricing_gap=sig.overpricing_gap,
                            total_event_excess=sig.total_event_excess,
                            yes_depth=sig.yes_depth,
                            yes_bid=sig.yes_bid,
                            yes_ask=sig.yes_ask,
                            spread=sig.spread,
                            bid_depth=sig.bid_depth,
                        )

                        # Dedup paper trade: one per bucket_ticker, with 24h cooldown
                        if db_logger.has_open_or_recent_paper_trade(
                            sig.bucket_ticker, self._paper_trade_cooldown
                        ):
                            continue

                        # Liquidity filters: skip illiquid buckets
                        if sig.bid_depth < MIN_BID_DEPTH:
                            logger.debug(
                                "SKIP (low depth): %s | bid_depth=%d < %d",
                                sig.bucket_ticker, sig.bid_depth, MIN_BID_DEPTH,
                            )
                            continue
                        if sig.spread > MAX_SPREAD:
                            logger.debug(
                                "SKIP (wide spread): %s | spread=%dc > %dc",
                                sig.bucket_ticker, sig.spread, MAX_SPREAD,
                            )
                            continue

                        self.stats["paper_trades"] += 1

                        logger.warning(
                            "PAPER TRADE: %s | %s | ask=%dc bid=%dc spread=%dc fair=%dc gap=+%dc | bid_depth=%d",
                            sig.event_title[:50], sig.bucket_label[:40],
                            sig.yes_ask, sig.yes_bid, sig.spread,
                            sig.fair_value_est, sig.overpricing_gap,
                            sig.bid_depth,
                        )

                        db_logger.log_paper_trade(
                            event_ticker=sig.event_ticker,
                            series_ticker=sig.series_ticker,
                            event_title=sig.event_title,
                            bucket_ticker=sig.bucket_ticker,
                            bucket_label=sig.bucket_label,
                            category=sig.category,
                            signal_type=signal_type,
                            entry_price=sig.current_price,
                            fair_value_est=sig.fair_value_est,
                            overpricing_gap=sig.overpricing_gap,
                            total_event_excess=sig.total_event_excess,
                            yes_depth=sig.yes_depth,
                            yes_bid=sig.yes_bid,
                            yes_ask=sig.yes_ask,
                            spread=sig.spread,
                            bid_depth=sig.bid_depth,
                        )

                        # Live execution: place limit sell at yes_bid
                        if config.LIVE_EXECUTION and not config.READ_ONLY:
                            self._execute_live_order(sig)

                    # Log near misses (with separate cooldown)
                    for nm in near_misses:
                        last_nm = self._recent_near_misses.get(nm["bucket_ticker"], 0)
                        if time.time() - last_nm < self._near_miss_cooldown:
                            continue
                        self._recent_near_misses[nm["bucket_ticker"]] = time.time()
                        db_logger.log_paper_near_miss(**nm)

        if self.scan_count % 12 == 0:  # Every ~60s at 5s interval
            logger.info(
                "Scan #%d — events=%d signals=%d paper=%d resolved=%d",
                self.scan_count, self.stats["events_checked"],
                self.stats["signals_found"], self.stats["paper_trades"],
                self.stats["resolved_trades"],
            )

        # Clean up old near-miss dedup entries
        nm_cutoff = time.time() - self._near_miss_cooldown * 2
        self._recent_near_misses = {
            k: v for k, v in self._recent_near_misses.items() if v > nm_cutoff
        }
