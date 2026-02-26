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


def _maker_fee(price_cents: int, maker_mult: float = 0.0175) -> float:
    """Kalshi maker fee: mult * P * (1-P), where P = price/100.

    Returns fee in cents for one contract on one leg.
    """
    p = price_cents / 100.0
    return maker_mult * p * (1.0 - p) * 100.0  # convert back to cents


def _maker_profit(yes_ask: int, no_ask: int, maker_mult: float = 0.0175) -> float:
    """Net profit in cents per contract using maker fees on both legs."""
    gross = 100 - yes_ask - no_ask
    fee_leg1 = _maker_fee(yes_ask, maker_mult)
    fee_leg2 = _maker_fee(no_ask, maker_mult)
    return gross - fee_leg1 - fee_leg2


class ArbBot:
    def __init__(self):
        self.traded_tickers: set[str] = set()
        self.running = False
        self.paused_until: float = 0
        # Rolling window: deque of bools (True = success, False = orphan)
        self.results: deque[bool] = deque(maxlen=config.WINDOW_SIZE)
        # Ladder snapshot cache: expiry -> deque of recent snapshots
        self.snapshot_cache: dict[str, deque] = {}
        self.last_summary_time: float = 0.0
        self.scan_count: int = 0
        self.cycle_counter: dict[str, int] = {s: 0 for s in config.SERIES}

    def start(self):
        self.running = True
        logger.info("Bot started — scanning %s series", list(config.SERIES.keys()))
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
        self.scan_count += 1
        cycle_start = time.monotonic()
        timing = {}  # series -> {api_ms, ladder_ms, detect_ms, db_ms}

        for series, fee_cfg in config.SERIES.items():
            taker_fee = fee_cfg["taker_fee"]
            maker_mult = fee_cfg["maker_mult"]
            contract_type = fee_cfg.get("contract_type", "above_below")
            is_arb_eligible = contract_type == "above_below"

            # Check per-series poll interval
            poll_every = fee_cfg.get("poll_every", 1)
            self.cycle_counter[series] = self.cycle_counter.get(series, 0) + 1
            if self.cycle_counter[series] % poll_every != 0:
                continue

            t0 = time.monotonic()
            markets = kalshi_api.get_markets(series, status="open")
            t_api = time.monotonic()
            logger.info("Found %d open markets for %s", len(markets), series)

            if not markets:
                continue

            # Housekeeping: clear traded tickers for markets that are no longer open
            open_tickers = {m["ticker"] for m in markets}
            settled = self.traded_tickers - open_tickers
            if settled:
                logger.info("Clearing settled tickers: %s", settled)
                self.traded_tickers -= settled

            if not config.READ_ONLY and not self._check_exposure():
                logger.warning("Max exposure reached, skipping scan")
                return

            # Log event durations on first cycle
            if not hasattr(self, "_logged_event_durations"):
                self._logged_event_durations = set()
            for m in markets[:1]:  # just check first market per series
                ct = m.get("close_time", "")
                ot = m.get("open_time", "")
                evt = m.get("event_ticker", "")
                if ct and ot and evt not in self._logged_event_durations:
                    self._logged_event_durations.add(evt)
                    try:
                        ct_ts = _parse_expiry_timestamp(ct)
                        ot_ts = _parse_expiry_timestamp(ot)
                        dur_h = (ct_ts - ot_ts) / 3600
                        logger.info(
                            "%s event %s: duration=%.1fh close=%s",
                            series, evt, dur_h, ct,
                        )
                    except Exception:
                        pass

            # Build ladder snapshots for all expiry windows (timed)
            t_ladder_start = time.monotonic()
            ladders = build_ladder(markets)
            t_ladder = time.monotonic()
            scan_duration_ms = (t_ladder - t_ladder_start) * 1000

            t_detect_start = time.monotonic()

            # Cache + log + detect for each window
            for expiry, snapshot in ladders.items():
                self._cache_snapshot(expiry, snapshot)

                # Only run arb detection on above/below contracts
                if is_arb_eligible:
                    opportunities, stale_counts = detect_violations(
                        snapshot, taker_fee, config.SOFT_ARB_PROB_THRESHOLD
                    )
                    ranked = rank_opportunities(opportunities)
                else:
                    ranked = []
                    stale_counts = {}

                log_ladder(snapshot, ranked, series_ticker=series,
                           stale_counts=stale_counts)

                # ── DB logging: scan, snapshot, opportunities ──
                # expiry is now close_time (when trading stops), not settlement date
                now_ts = time.time()
                expiry_ts = _parse_expiry_timestamp(expiry)
                ttl = max(0.0, expiry_ts - now_ts) if expiry_ts > 0 else None

                try:
                    db_logger.log_scan(expiry, len(snapshot.strikes), scan_duration_ms,
                                       series_ticker=series)
                except Exception:
                    logger.warning("db_logger.log_scan failed", exc_info=True)

                try:
                    db_logger.log_snapshot(expiry, snapshot.strikes, series_ticker=series)
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

                        # Maker profit: use series-specific maker multiplier
                        maker_profit = _maker_profit(yes_ask_low, no_ask_high, maker_mult)

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
                            estimated_profit_maker=maker_profit,
                            series_ticker=series,
                            btc_price_at_detection=None,
                            time_to_expiry_seconds=ttl,
                            depth_thin_side=depth_thin,
                        )
                    except Exception:
                        logger.warning("db_logger.log_opportunity failed", exc_info=True)

                # ── Arb stability tracking ──
                hard_arbs = [
                    opp for opp in ranked if opp.type == "C_hard_arb"
                ]
                stability_arbs = []
                for opp in hard_arbs:
                    lo_s = strike_map.get(opp.strikes[0])
                    hi_s = strike_map.get(opp.strikes[1]) if len(opp.strikes) > 1 else None
                    depth = None
                    if lo_s and hi_s:
                        depth = min(lo_s.yes_ask_depth, hi_s.no_ask_depth)
                    stability_arbs.append({
                        "strike_low": opp.strikes[0],
                        "strike_high": opp.strikes[1] if len(opp.strikes) > 1 else opp.strikes[0],
                        "combined_cost": opp.legs[0]["price"] + (opp.legs[1]["price"] if len(opp.legs) > 1 else 0),
                        "depth_thin_side": depth,
                    })
                try:
                    db_logger.update_arb_stability(expiry, stability_arbs, series_ticker=series)
                except Exception:
                    logger.warning("db_logger.update_arb_stability failed", exc_info=True)

                # Execute single-market arb on each strike (scan-only in READ_ONLY)
                if not is_arb_eligible:
                    continue
                for strike in snapshot.strikes:
                    if strike.ticker in self.traded_tickers:
                        continue
                    combined = strike.yes_ask + strike.no_ask
                    if combined <= config.ARB_THRESHOLD:
                        if config.READ_ONLY:
                            logger.info(
                                "READ_ONLY: would execute arb on %s — yes=%d + no=%d = %d",
                                strike.ticker, strike.yes_ask, strike.no_ask, combined,
                            )
                        else:
                            self._execute_arb(
                                strike.ticker, strike.yes_ask,
                                strike.yes_ask_depth, strike.no_ask,
                                strike.no_ask_depth,
                                expiry_window=expiry,
                                strike_price=strike.strike,
                                taker_fee=taker_fee,
                            )

            t_end = time.monotonic()
            timing[series] = {
                "api_ms": (t_api - t0) * 1000,
                "ladder_ms": (t_ladder - t_ladder_start) * 1000,
                "detect_db_ms": (t_end - t_detect_start) * 1000,
                "total_ms": (t_end - t0) * 1000,
            }

        # Log timing breakdown every 10 cycles
        if self.scan_count % 10 == 0 and timing:
            cycle_ms = (time.monotonic() - cycle_start) * 1000
            parts = []
            for s, t in timing.items():
                parts.append(
                    f"{s}: api={t['api_ms']:.0f} ladder={t['ladder_ms']:.0f} "
                    f"detect+db={t['detect_db_ms']:.0f} total={t['total_ms']:.0f}"
                )
            logger.info(
                "=== CYCLE #%d TIMING (%.0fms total) === %s",
                self.scan_count, cycle_ms, " | ".join(parts),
            )

        # --- 30-minute maker strategy summary (once per cycle, after all series) ---
        now_mono = time.monotonic()
        if now_mono - self.last_summary_time >= 1800:
            self.last_summary_time = now_mono
            try:
                result = db_logger.get_maker_summary(window_seconds=1800)
                if result:
                    agg = result["all"]
                    logger.info(
                        "=== 30-MIN MAKER SUMMARY (ALL) === "
                        "hard_arbs=%d | profitable_taker=%d | profitable_maker=%d | "
                        "avg_gross_spread=%.1f cents | avg_depth_thin=%d contracts",
                        agg["total_hard_arbs"],
                        agg["profitable_taker"],
                        agg["profitable_maker"],
                        agg["avg_gross_spread_maker"],
                        int(agg["avg_depth_maker"]),
                    )
                    for s in result["per_series"]:
                        logger.info(
                            "  %s: hard_arbs=%d | taker_profit=%d | maker_profit=%d | "
                            "avg_spread=%.1f | avg_depth=%d",
                            s["series"], s["total_hard_arbs"],
                            s["profitable_taker"], s["profitable_maker"],
                            s["avg_gross_spread_maker"], int(s["avg_depth_maker"]),
                        )
            except Exception:
                logger.warning("Maker summary query failed", exc_info=True)

            # Stability summary
            try:
                stab = db_logger.get_stability_summary(window_seconds=1800)
                if stab:
                    logger.info(
                        "=== 30-MIN STABILITY === closed=%d | avg_scans=%.1f | avg_duration=%.0fs",
                        stab["total_closed"], stab["avg_scan_count"],
                        stab["avg_duration_seconds"],
                    )
            except Exception:
                logger.warning("Stability summary failed", exc_info=True)

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
                     expiry_window: str = "", strike_price: float = 0.0,
                     taker_fee: float = 0.07):
        now = datetime.now(timezone.utc).strftime("%H:%M:%S")
        count = config.MAX_CONTRACTS

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
                                expiry_window, strike_price, taker_fee)
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
                    fees=_maker_fee(yes_price, taker_fee) + _maker_fee(no_price, taker_fee),
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
                            expiry_window, strike_price, taker_fee)

    # ------------------------------------------------------------------
    # Orphan recovery — exit the filled leg immediately
    # ------------------------------------------------------------------

    def _handle_orphan(self, ticker: str, filled_side: str, fill_price: int,
                       count: int, now: str,
                       expiry_window: str = "", strike_price: float = 0.0,
                       taker_fee: float = 0.07):
        """Exit the orphaned position by selling at best bid."""
        self.results.append(False)

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
                        fees=_maker_fee(fill_price, taker_fee),
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
