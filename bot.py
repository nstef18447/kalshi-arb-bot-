import time
import logging
import threading
from collections import deque
from datetime import datetime, timezone

import config
import db_logger
import kalshi_api
from scanner import build_ladder, detect_violations, rank_opportunities, log_ladder

logger = logging.getLogger("arb-bot")


def _parse_expiry_timestamp(expiry_str: str) -> float:
    """Parse ISO expiry string to unix timestamp. Returns 0 on failure."""
    try:
        # Handle formats like "2026-02-23T12:15:00Z"
        dt = datetime.fromisoformat(expiry_str.replace("Z", "+00:00"))
        return dt.timestamp()
    except Exception:
        return 0.0


def _split_opp_type(type_str: str) -> tuple[str, str]:
    """Split TradeOpportunity.type into (opp_type, sub_type) for the DB.

    'A_monotonicity'    -> ('A', 'monotonicity')
    'B_probability_gap' -> ('B', 'probability_gap')
    'C_hard_arb'        -> ('C', 'hard')
    'C_soft_arb'        -> ('C', 'soft')
    """
    if type_str == "C_hard_arb":
        return "C", "hard"
    if type_str == "C_soft_arb":
        return "C", "soft"
    parts = type_str.split("_", 1)
    if len(parts) == 2:
        return parts[0], parts[1]
    return type_str, ""


def _is_filled(order: dict) -> bool:
    return order.get("status") == "filled" or order.get("remaining_count", 1) == 0


def _wait_for_fill(order_id: str, timeout: float) -> dict:
    """Poll an order until filled or timeout. Returns final order state."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        order = kalshi_api.get_order(order_id)
        if _is_filled(order):
            return order
        time.sleep(0.3)
    return kalshi_api.get_order(order_id)


class ArbBot:
    def __init__(self):
        self.traded_tickers: set[str] = set()
        self.running = False
        self.paused_until: float = 0
        # Rolling window: deque of bools (True = success, False = orphan)
        self.results: deque[bool] = deque(maxlen=config.WINDOW_SIZE)
        # Ladder snapshot cache: expiry -> deque of recent snapshots
        self.snapshot_cache: dict[str, deque] = {}

    def start(self):
        self.running = True
        logger.info("Bot started — scanning %s series", config.SERIES)
        while self.running:
            try:
                if time.monotonic() < self.paused_until:
                    remaining = int(self.paused_until - time.monotonic())
                    logger.info("Circuit breaker active — %ds remaining", remaining)
                    time.sleep(config.POLL_INTERVAL)
                    continue
                self._scan_cycle()
            except KeyboardInterrupt:
                break
            except Exception:
                logger.exception("Error in scan cycle")
            time.sleep(config.POLL_INTERVAL)

    def stop(self):
        self.running = False
        logger.info("Bot stopping...")

    # ------------------------------------------------------------------
    # Scan cycle — unchanged entry criteria
    # ------------------------------------------------------------------

    def _scan_cycle(self):
        for series in config.SERIES:
            markets = kalshi_api.get_markets(series, status="open")
            logger.info("Found %d open markets for %s", len(markets), series)

            # Housekeeping: clear traded tickers for markets that are no longer open
            open_tickers = {m["ticker"] for m in markets}
            settled = self.traded_tickers - open_tickers
            if settled:
                logger.info("Clearing settled tickers: %s", settled)
                self.traded_tickers -= settled

            if not self._check_exposure():
                logger.warning("Max exposure reached, skipping scan")
                return

            # Build ladder snapshots for all expiry windows (timed)
            scan_start = time.monotonic()
            ladders = build_ladder(markets)
            scan_duration_ms = (time.monotonic() - scan_start) * 1000

            # Cache + log + detect for each window
            for expiry, snapshot in ladders.items():
                self._cache_snapshot(expiry, snapshot)
                opportunities = detect_violations(
                    snapshot, config.FEE_RATE, config.SOFT_ARB_PROB_THRESHOLD
                )
                ranked = rank_opportunities(opportunities)
                log_ladder(snapshot, ranked)

                # ── DB logging: scan, snapshot, opportunities ──
                now_ts = time.time()
                expiry_ts = _parse_expiry_timestamp(expiry)
                ttl = max(0.0, expiry_ts - now_ts) if expiry_ts > 0 else None

                try:
                    db_logger.log_scan(expiry, len(snapshot.strikes), scan_duration_ms)
                except Exception:
                    logger.warning("db_logger.log_scan failed", exc_info=True)

                try:
                    db_logger.log_snapshot(expiry, snapshot.strikes)
                except Exception:
                    logger.warning("db_logger.log_snapshot failed", exc_info=True)

                # Build strike lookup for depth info
                strike_map = {s.strike: s for s in snapshot.strikes}

                for opp in ranked:
                    try:
                        opp_type, sub_type = _split_opp_type(opp.type)
                        strike_low = opp.strikes[0]
                        strike_high = opp.strikes[1] if len(opp.strikes) > 1 else opp.strikes[0]

                        # Resolve prices from legs
                        yes_ask_low = opp.legs[0]["price"] if opp.legs else 0
                        no_ask_high = opp.legs[1]["price"] if len(opp.legs) > 1 else 0
                        combined_cost = yes_ask_low + no_ask_high

                        # Depth of the thinner side
                        lo_strike = strike_map.get(strike_low)
                        hi_strike = strike_map.get(strike_high)
                        depth_thin = None
                        if lo_strike and hi_strike:
                            depth_thin = min(lo_strike.yes_ask_depth, hi_strike.no_ask_depth)

                        db_logger.log_opportunity(
                            expiry_window=expiry,
                            opp_type=opp_type,
                            sub_type=sub_type,
                            strike_low=strike_low,
                            strike_high=strike_high,
                            yes_ask_low=yes_ask_low,
                            no_ask_high=no_ask_high,
                            combined_cost=combined_cost,
                            estimated_profit=opp.net_profit_cents,
                            btc_price_at_detection=None,
                            time_to_expiry_seconds=ttl,
                            depth_thin_side=depth_thin,
                        )
                    except Exception:
                        logger.warning("db_logger.log_opportunity failed", exc_info=True)

                # Still run existing single-market arb on each strike
                for strike in snapshot.strikes:
                    if strike.ticker in self.traded_tickers:
                        continue
                    combined = strike.yes_ask + strike.no_ask
                    if combined <= config.ARB_THRESHOLD:
                        self._execute_arb(
                            strike.ticker, strike.yes_ask,
                            strike.yes_ask_depth, strike.no_ask,
                            strike.no_ask_depth,
                            expiry_window=expiry,
                            strike_price=strike.strike,
                        )

    def _cache_snapshot(self, expiry: str, snapshot):
        """Store snapshot in rolling cache for trend analysis."""
        if expiry not in self.snapshot_cache:
            self.snapshot_cache[expiry] = deque(maxlen=config.SNAPSHOT_CACHE_SIZE)
        self.snapshot_cache[expiry].append(snapshot)

    # ------------------------------------------------------------------
    # Execution — sequential leg placement with all 5 improvements
    # ------------------------------------------------------------------

    def _execute_arb(self, ticker: str, yes_price: int, yes_depth: int,
                     no_price: int, no_depth: int,
                     expiry_window: str = "", strike_price: float = 0.0):
        now = datetime.now(timezone.utc).strftime("%H:%M:%S")
        count = config.MAX_CONTRACTS
        fee_per_leg = config.FEE_RATE * 100  # cents

        # --- 1. PRE-FLIGHT LIQUIDITY CHECK ---
        if yes_depth < config.MIN_DEPTH:
            logger.info(
                "[%s] %s SKIP: Yes depth %d < MIN_DEPTH %d",
                now, ticker, yes_depth, config.MIN_DEPTH,
            )
            return
        if no_depth < config.MIN_DEPTH:
            logger.info(
                "[%s] %s SKIP: No depth %d < MIN_DEPTH %d",
                now, ticker, no_depth, config.MIN_DEPTH,
            )
            return

        # Block ticker to prevent duplicates
        self.traded_tickers.add(ticker)

        # --- 2. LEG THE ILLIQUID SIDE FIRST ---
        if yes_depth <= no_depth:
            first_side, first_price, first_depth = "yes", yes_price, yes_depth
            second_side, second_price, second_depth = "no", no_price, no_depth
        else:
            first_side, first_price, first_depth = "no", no_price, no_depth
            second_side, second_price, second_depth = "yes", yes_price, yes_depth

        logger.info(
            "[%s] %s EXEC: first=%s@%d(depth=%d) second=%s@%d(depth=%d) | reason=thinner_side",
            now, ticker, first_side, first_price, first_depth,
            second_side, second_price, second_depth,
        )

        # Place first leg (illiquid side)
        try:
            first_order = kalshi_api.create_order(ticker, first_side, first_price, count)
        except Exception:
            logger.exception("[%s] %s FAIL: could not place first leg (%s)", now, ticker, first_side)
            self.traded_tickers.discard(ticker)
            return

        first_id = first_order.get("order_id", "")
        logger.info("[%s] %s FIRST LEG placed: %s order_id=%s", now, ticker, first_side, first_id)

        # Wait for first leg fill
        first_final = _wait_for_fill(first_id, config.FIRST_LEG_TIMEOUT)

        if not _is_filled(first_final):
            # First leg didn't fill — cancel and abort, no exposure
            self._safe_cancel(first_id)
            self.traded_tickers.discard(ticker)
            logger.info(
                "[%s] %s ABORT: first leg %s did not fill in %ds — cancelled, no exposure",
                now, ticker, first_side, config.FIRST_LEG_TIMEOUT,
            )
            try:
                db_logger.log_trade(
                    expiry_window=expiry_window, opp_type="single",
                    strike_low=strike_price, strike_high=strike_price,
                    leg1_side=first_side, leg1_price=first_price,
                    leg1_fill_status="cancelled",
                )
            except Exception:
                logger.warning("db_logger.log_trade failed", exc_info=True)
            return

        logger.info("[%s] %s FIRST LEG FILLED: %s@%d", now, ticker, first_side, first_price)

        # --- First leg filled — place second leg immediately ---
        try:
            second_order = kalshi_api.create_order(ticker, second_side, second_price, count)
        except Exception:
            logger.exception("[%s] %s FAIL: could not place second leg (%s)", now, ticker, second_side)
            self._handle_orphan(ticker, first_side, first_price, count, now,
                                expiry_window, strike_price)
            return

        second_id = second_order.get("order_id", "")
        logger.info("[%s] %s SECOND LEG placed: %s order_id=%s", now, ticker, second_side, second_id)

        # Wait for second leg fill
        second_final = _wait_for_fill(second_id, config.SECOND_LEG_TIMEOUT)

        if _is_filled(second_final):
            # --- SUCCESS: both legs filled ---
            profit = 100 - yes_price - no_price
            self.results.append(True)
            logger.info(
                "[%s] %s SUCCESS: both legs filled — %d cents/contract profit | "
                "orphan_rate=%.2f (%d/%d)",
                now, ticker, profit, self._orphan_rate(),
                self._orphan_count(), len(self.results),
            )
            try:
                db_logger.log_trade(
                    expiry_window=expiry_window, opp_type="single",
                    strike_low=strike_price, strike_high=strike_price,
                    leg1_side=first_side, leg1_price=first_price,
                    leg1_fill_status="filled",
                    leg2_side=second_side, leg2_price=second_price,
                    leg2_fill_status="filled",
                    orphaned=False,
                    realized_pnl=float(profit * count),
                    fees=fee_per_leg * 2,
                )
            except Exception:
                logger.warning("db_logger.log_trade failed", exc_info=True)
            return

        # --- 3. ORPHAN FILL RECOVERY: second leg didn't fill ---
        logger.warning(
            "[%s] %s ORPHAN: %s filled but %s did not fill in %ds",
            now, ticker, first_side, second_side, config.SECOND_LEG_TIMEOUT,
        )
        self._safe_cancel(second_id)
        self._handle_orphan(ticker, first_side, first_price, count, now,
                            expiry_window, strike_price)

    # ------------------------------------------------------------------
    # Orphan recovery — exit the filled leg immediately
    # ------------------------------------------------------------------

    def _handle_orphan(self, ticker: str, filled_side: str, fill_price: int,
                       count: int, now: str,
                       expiry_window: str = "", strike_price: float = 0.0):
        """Exit the orphaned position by selling at best bid."""
        self.results.append(False)
        fee_per_leg = config.FEE_RATE * 100

        # Fetch current book to get best bid for our side
        try:
            book = kalshi_api.get_orderbook(ticker, depth=1)
        except Exception:
            logger.exception(
                "[%s] %s ORPHAN EXIT FAILED: could not fetch orderbook for exit", now, ticker
            )
            try:
                db_logger.log_trade(
                    expiry_window=expiry_window, opp_type="single",
                    strike_low=strike_price, strike_high=strike_price,
                    leg1_side=filled_side, leg1_price=fill_price,
                    leg1_fill_status="filled",
                    orphaned=True,
                )
            except Exception:
                logger.warning("db_logger.log_trade failed", exc_info=True)
            self._check_circuit_breaker(now)
            return

        # Kalshi returns bids only. Best Yes bid = yes[0][0], Best No bid = no[0][0]
        # To sell Yes, we need the best Yes bid (someone willing to buy our Yes)
        # To sell No, we need the best No bid
        if filled_side == "yes":
            yes_bids = book.get("yes", [])
            exit_price = yes_bids[0][0] if yes_bids else 1
        else:
            no_bids = book.get("no", [])
            exit_price = no_bids[0][0] if no_bids else 1

        logger.warning(
            "[%s] %s ORPHAN EXIT: selling %d %s contracts — fill_price=%d exit_price=%d",
            now, ticker, count, filled_side, fill_price, exit_price,
        )

        try:
            sell_order = kalshi_api.create_sell_order(ticker, filled_side, exit_price, count)
            sell_id = sell_order.get("order_id", "")
            # Give the sell a few seconds to fill
            sell_final = _wait_for_fill(sell_id, config.SECOND_LEG_TIMEOUT)
            if _is_filled(sell_final):
                realized_pnl = exit_price - fill_price
                logger.warning(
                    "[%s] %s ORPHAN CLOSED: %s sold@%d (bought@%d) | P&L=%d cents/contract (%+d total) | "
                    "orphan_rate=%.2f (%d/%d)",
                    now, ticker, filled_side, exit_price, fill_price,
                    realized_pnl, realized_pnl * count,
                    self._orphan_rate(), self._orphan_count(), len(self.results),
                )
                try:
                    db_logger.log_trade(
                        expiry_window=expiry_window, opp_type="single",
                        strike_low=strike_price, strike_high=strike_price,
                        leg1_side=filled_side, leg1_price=fill_price,
                        leg1_fill_status="filled",
                        orphaned=True,
                        exit_price=exit_price,
                        realized_pnl=float(realized_pnl * count),
                        fees=fee_per_leg,
                    )
                except Exception:
                    logger.warning("db_logger.log_trade failed", exc_info=True)
            else:
                self._safe_cancel(sell_id)
                logger.error(
                    "[%s] %s ORPHAN EXIT INCOMPLETE: sell order %s did not fill — "
                    "MANUAL INTERVENTION REQUIRED | holding %d %s@%d",
                    now, ticker, sell_id, count, filled_side, fill_price,
                )
                try:
                    db_logger.log_trade(
                        expiry_window=expiry_window, opp_type="single",
                        strike_low=strike_price, strike_high=strike_price,
                        leg1_side=filled_side, leg1_price=fill_price,
                        leg1_fill_status="filled",
                        orphaned=True,
                    )
                except Exception:
                    logger.warning("db_logger.log_trade failed", exc_info=True)
        except Exception:
            logger.exception(
                "[%s] %s ORPHAN EXIT FAILED: could not place sell order — "
                "MANUAL INTERVENTION REQUIRED | holding %d %s@%d",
                now, ticker, count, filled_side, fill_price,
            )
            try:
                db_logger.log_trade(
                    expiry_window=expiry_window, opp_type="single",
                    strike_low=strike_price, strike_high=strike_price,
                    leg1_side=filled_side, leg1_price=fill_price,
                    leg1_fill_status="filled",
                    orphaned=True,
                )
            except Exception:
                logger.warning("db_logger.log_trade failed", exc_info=True)

        self._check_circuit_breaker(now)

    # ------------------------------------------------------------------
    # Circuit breaker
    # ------------------------------------------------------------------

    def _orphan_count(self) -> int:
        return sum(1 for r in self.results if not r)

    def _orphan_rate(self) -> float:
        if not self.results:
            return 0.0
        return self._orphan_count() / len(self.results)

    def _check_circuit_breaker(self, now: str):
        """Trip circuit breaker if orphan rate exceeds threshold."""
        rate = self._orphan_rate()
        if len(self.results) >= 5 and rate > config.MAX_ORPHAN_RATE:
            self.paused_until = time.monotonic() + (config.COOLDOWN_MINUTES * 60)
            logger.warning(
                "[%s] CIRCUIT BREAKER TRIPPED: orphan_rate=%.2f (%d/%d) > %.2f — "
                "pausing for %d minutes",
                now, rate, self._orphan_count(), len(self.results),
                config.MAX_ORPHAN_RATE, config.COOLDOWN_MINUTES,
            )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _safe_cancel(self, order_id: str):
        try:
            kalshi_api.cancel_order(order_id)
            logger.info("Cancelled order %s", order_id)
        except Exception:
            logger.debug("Could not cancel order %s (may already be filled/cancelled)", order_id)

    def _check_exposure(self) -> bool:
        try:
            positions = kalshi_api.get_positions()
            total_exposure = 0
            for pos in positions:
                yes_count = abs(pos.get("market_exposure", 0))
                total_exposure += yes_count

            if total_exposure >= config.MAX_EXPOSURE:
                logger.warning(
                    "Exposure %d cents >= limit %d cents", total_exposure, config.MAX_EXPOSURE
                )
                return False
            return True
        except Exception:
            logger.exception("Failed to check exposure")
            return True
