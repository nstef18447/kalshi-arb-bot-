"""Binary arb bot for KXBTC15M 15-minute binary contracts.

Scans for same-market yes+no mispricing (combined ask < $1.00).
When found, buys both sides to lock in risk-free profit.
Includes delayed hedge check to handle partial/one-sided fills.
"""

import time
import logging

import config
import db_logger
import kalshi_api

logger = logging.getLogger("binary-arb")

SERIES = "KXBTC15M"
TAKER_FEE = config.SERIES[SERIES]["taker_fee"]


def _is_filled(order: dict) -> bool:
    return order.get("status") == "filled" or order.get("remaining_count", 1) == 0


def _fill_count(order: dict) -> int:
    """Return number of contracts filled."""
    total = order.get("count", 0)
    remaining = order.get("remaining_count", 0)
    return total - remaining


class BinaryArbBot:
    def __init__(self):
        self.running = False
        self.cooldowns: dict[str, float] = {}
        self.scan_count = 0
        self.pending_hedges: list[dict] = []
        self.stats = {"scans": 0, "opportunities": 0, "trades": 0, "hedges": 0}

    def start(self):
        self.running = True
        logger.info("Binary arb bot started — scanning %s", SERIES)
        while self.running:
            try:
                self._process_hedges()
                self._scan_cycle()
            except KeyboardInterrupt:
                break
            except Exception:
                logger.exception("Error in binary arb scan cycle")
            time.sleep(config.POLL_INTERVAL)

    def stop(self):
        self.running = False
        logger.info("Binary arb bot stopping — processing remaining hedges...")
        # Force-check all pending hedges
        for hedge in list(self.pending_hedges):
            hedge["check_at"] = 0
        self._process_hedges()
        logger.info(
            "Binary arb bot stopped — stats: %s", self.stats
        )

    def _scan_cycle(self):
        self.scan_count += 1
        self.stats["scans"] += 1

        markets = kalshi_api.get_markets(SERIES, status="open")
        logger.info("[scan #%d] Found %d open %s markets", self.scan_count, len(markets), SERIES)

        if not markets:
            return

        now = time.monotonic()
        scanned = 0
        empty_books = 0

        for market in markets:
            ticker = market.get("ticker", "")
            if not ticker:
                continue

            # Cooldown check
            last_trade = self.cooldowns.get(ticker, 0)
            if now - last_trade < config.BINARY_ARB_COOLDOWN:
                continue

            try:
                book = kalshi_api.get_orderbook(ticker, depth=1)
            except Exception:
                logger.warning("Could not fetch orderbook for %s", ticker)
                continue

            scanned += 1

            # Parse book: Kalshi returns yes bids and no bids
            # To BUY yes: yes_ask = 100 - best_no_bid
            # To BUY no:  no_ask  = 100 - best_yes_bid
            yes_bids = book.get("yes", [])
            no_bids = book.get("no", [])

            if not yes_bids or not no_bids:
                empty_books += 1
                continue

            yes_ask = 100 - no_bids[0][0]
            no_ask = 100 - yes_bids[0][0]
            yes_depth = no_bids[0][1]
            no_depth = yes_bids[0][1]

            combined = yes_ask + no_ask
            would_trade = (combined <= config.BINARY_ARB_THRESHOLD and
                           yes_depth >= config.BINARY_ARB_SIZE and
                           no_depth >= config.BINARY_ARB_SIZE)

            # Print every market with a book to stdout in dry-run
            if config.READ_ONLY:
                trade_flag = ">>> WOULD TRADE <<<" if would_trade else ""
                print(
                    f"  {ticker:30s}  yes_ask={yes_ask:3d}  no_ask={no_ask:3d}  "
                    f"combined={combined:3d}  yes_depth={yes_depth:4d}  no_depth={no_depth:4d}  "
                    f"{trade_flag}"
                )

            if would_trade:
                self.stats["opportunities"] += 1
                logger.info(
                    "[scan #%d] %s OPPORTUNITY: yes=%d + no=%d = %d (thresh=%d) "
                    "depth_yes=%d depth_no=%d",
                    self.scan_count, ticker, yes_ask, no_ask, combined,
                    config.BINARY_ARB_THRESHOLD, yes_depth, no_depth,
                )

                if config.READ_ONLY:
                    # Log to DB as dry-run observation (no order IDs)
                    try:
                        row_id = db_logger.log_binary_arb_trade(
                            ticker=ticker,
                            yes_price=yes_ask,
                            no_price=no_ask,
                            combined_cost=combined,
                            size=config.BINARY_ARB_SIZE,
                        )
                        db_logger.update_binary_arb_trade(
                            row_id, yes_filled=0, no_filled=0,
                            hedge_action="dry_run", realized_pnl=0, fees=0,
                        )
                    except Exception:
                        logger.warning("db_logger dry-run log failed", exc_info=True)
                else:
                    self._execute_binary_arb(
                        ticker, yes_ask, no_ask, yes_depth, no_depth
                    )

        if config.READ_ONLY:
            print(
                f"  --- scan #{self.scan_count}: {len(markets)} markets, "
                f"{scanned} books fetched, {empty_books} empty, "
                f"{self.stats['opportunities']} total opportunities ---"
            )

    def _execute_binary_arb(self, ticker: str, yes_ask: int, no_ask: int,
                            yes_depth: int, no_depth: int):
        size = config.BINARY_ARB_SIZE
        combined = yes_ask + no_ask

        logger.info(
            "%s EXEC: yes=%d no=%d combined=%d size=%d",
            ticker, yes_ask, no_ask, combined, size,
        )

        # Place NO order first (typically thinner side)
        try:
            no_order = kalshi_api.create_order(ticker, "no", no_ask, size)
        except Exception:
            logger.exception("%s FAIL: could not place NO order", ticker)
            return

        no_order_id = no_order.get("order_id", "")

        # Place YES order immediately
        try:
            yes_order = kalshi_api.create_order(ticker, "yes", yes_ask, size)
        except Exception:
            logger.exception("%s FAIL: could not place YES order — cancelling NO", ticker)
            self._safe_cancel(no_order_id)
            return

        yes_order_id = yes_order.get("order_id", "")

        # Set cooldown
        self.cooldowns[ticker] = time.monotonic()
        self.stats["trades"] += 1

        # Log to DB
        try:
            row_id = db_logger.log_binary_arb_trade(
                ticker=ticker,
                yes_price=yes_ask,
                no_price=no_ask,
                combined_cost=combined,
                size=size,
                yes_order_id=yes_order_id,
                no_order_id=no_order_id,
            )
        except Exception:
            logger.warning("db_logger.log_binary_arb_trade failed", exc_info=True)
            row_id = None

        # Schedule hedge check
        self.pending_hedges.append({
            "ticker": ticker,
            "yes_order_id": yes_order_id,
            "no_order_id": no_order_id,
            "yes_price": yes_ask,
            "no_price": no_ask,
            "size": size,
            "check_at": time.monotonic() + config.BINARY_ARB_HEDGE_DELAY,
            "db_row_id": row_id,
        })

        logger.info(
            "%s ORDERS PLACED: yes_id=%s no_id=%s — hedge check in %ds",
            ticker, yes_order_id, no_order_id, config.BINARY_ARB_HEDGE_DELAY,
        )

    def _process_hedges(self):
        now = time.monotonic()
        remaining = []

        for hedge in self.pending_hedges:
            if now < hedge["check_at"]:
                remaining.append(hedge)
                continue

            self.stats["hedges"] += 1
            ticker = hedge["ticker"]
            yes_oid = hedge["yes_order_id"]
            no_oid = hedge["no_order_id"]
            yes_price = hedge["yes_price"]
            no_price = hedge["no_price"]
            size = hedge["size"]
            row_id = hedge["db_row_id"]

            try:
                yes_order = kalshi_api.get_order(yes_oid)
                no_order = kalshi_api.get_order(no_oid)
            except Exception:
                logger.exception("%s HEDGE CHECK FAILED: could not fetch orders", ticker)
                remaining.append(hedge)
                hedge["check_at"] = now + 5  # retry in 5s
                continue

            yes_filled = _is_filled(yes_order)
            no_filled = _is_filled(no_order)
            yes_fill_count = _fill_count(yes_order)
            no_fill_count = _fill_count(no_order)

            if yes_filled and no_filled:
                # Clean arb — both sides filled
                gross = (100 - yes_price - no_price) * size
                fees = (TAKER_FEE * yes_price / 100 * size * 100) + \
                       (TAKER_FEE * no_price / 100 * size * 100)
                pnl = gross - fees
                logger.info(
                    "%s CLEAN ARB: both filled — gross=%d fees=%.1f pnl=%.1f cents",
                    ticker, gross, fees, pnl,
                )
                self._update_db(row_id, size, size, "clean", pnl, fees)

            elif yes_filled and not no_filled:
                # YES filled, NO didn't — cancel NO, sell YES
                logger.warning("%s PARTIAL: YES filled, NO not — unwinding", ticker)
                self._safe_cancel(no_oid)
                self._unwind_position(ticker, "yes", yes_price, yes_fill_count, row_id,
                                      no_fill_count)

            elif no_filled and not yes_filled:
                # NO filled, YES didn't — cancel YES, sell NO
                logger.warning("%s PARTIAL: NO filled, YES not — unwinding", ticker)
                self._safe_cancel(yes_oid)
                self._unwind_position(ticker, "no", no_price, no_fill_count, row_id,
                                      yes_fill_count)

            else:
                # Neither filled
                logger.info("%s MISSED: neither side filled — cancelling both", ticker)
                self._safe_cancel(yes_oid)
                self._safe_cancel(no_oid)
                self._update_db(row_id, 0, 0, "missed", 0, 0)

        self.pending_hedges = remaining

    def _unwind_position(self, ticker: str, filled_side: str, fill_price: int,
                         filled_qty: int, row_id, other_fill_count: int):
        """Sell a one-sided fill at market best bid."""
        if filled_qty <= 0:
            self._update_db(row_id, 0, 0, "missed", 0, 0)
            return

        try:
            book = kalshi_api.get_orderbook(ticker, depth=1)
        except Exception:
            logger.exception(
                "%s UNWIND FAILED: could not fetch orderbook — MANUAL INTERVENTION",
                ticker,
            )
            if filled_side == "yes":
                self._update_db(row_id, filled_qty, other_fill_count, "unwind_failed", None, None)
            else:
                self._update_db(row_id, other_fill_count, filled_qty, "unwind_failed", None, None)
            return

        # Get best bid for the filled side
        if filled_side == "yes":
            yes_bids = book.get("yes", [])
            exit_price = yes_bids[0][0] if yes_bids else 1
        else:
            no_bids = book.get("no", [])
            exit_price = no_bids[0][0] if no_bids else 1

        logger.warning(
            "%s UNWIND: selling %d %s@%d (bought@%d) exit@%d",
            ticker, filled_qty, filled_side, fill_price, fill_price, exit_price,
        )

        try:
            sell_order = kalshi_api.create_sell_order(ticker, filled_side, exit_price, filled_qty)
            sell_id = sell_order.get("order_id", "")
            # Wait briefly for sell to fill
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                sell_status = kalshi_api.get_order(sell_id)
                if _is_filled(sell_status):
                    break
                time.sleep(0.3)
            else:
                sell_status = kalshi_api.get_order(sell_id)

            if _is_filled(sell_status):
                pnl = (exit_price - fill_price) * filled_qty
                fees = TAKER_FEE * fill_price / 100 * filled_qty * 100
                logger.warning(
                    "%s UNWIND COMPLETE: %s sold@%d pnl=%d fees=%.1f",
                    ticker, filled_side, exit_price, pnl, fees,
                )
            else:
                self._safe_cancel(sell_id)
                pnl = None
                fees = None
                logger.error(
                    "%s UNWIND INCOMPLETE: sell didn't fill — MANUAL INTERVENTION",
                    ticker,
                )
        except Exception:
            logger.exception("%s UNWIND FAILED: could not place sell", ticker)
            pnl = None
            fees = None

        if filled_side == "yes":
            self._update_db(row_id, filled_qty, other_fill_count, "unwound", pnl, fees)
        else:
            self._update_db(row_id, other_fill_count, filled_qty, "unwound", pnl, fees)

    def _update_db(self, row_id, yes_filled, no_filled, hedge_action, pnl, fees):
        if row_id is None:
            return
        try:
            db_logger.update_binary_arb_trade(
                row_id, yes_filled, no_filled, hedge_action, pnl, fees
            )
        except Exception:
            logger.warning("db update failed for row %s", row_id, exc_info=True)

    def _safe_cancel(self, order_id: str):
        try:
            kalshi_api.cancel_order(order_id)
            logger.info("Cancelled order %s", order_id)
        except Exception:
            logger.debug("Could not cancel order %s (may already be filled/cancelled)", order_id)
