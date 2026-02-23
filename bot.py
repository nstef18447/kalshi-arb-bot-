import time
import logging
import threading

import config
import kalshi_api

logger = logging.getLogger("arb-bot")


class ArbBot:
    def __init__(self):
        self.traded_tickers: set[str] = set()
        self.running = False

    def start(self):
        self.running = True
        logger.info("Bot started — scanning %s series", config.SERIES)
        while self.running:
            try:
                self._scan_cycle()
            except KeyboardInterrupt:
                break
            except Exception:
                logger.exception("Error in scan cycle")
            time.sleep(config.POLL_INTERVAL)

    def stop(self):
        self.running = False
        logger.info("Bot stopping...")

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

            # Check exposure before scanning
            if not self._check_exposure():
                logger.warning("Max exposure reached, skipping scan")
                return

            for market in markets:
                ticker = market["ticker"]
                if ticker in self.traded_tickers:
                    continue

                self._evaluate_market(ticker)

    def _evaluate_market(self, ticker: str):
        """Check a single market for arb opportunity and execute if found."""
        try:
            book = kalshi_api.get_orderbook(ticker, depth=1)
        except Exception:
            logger.exception("Failed to fetch orderbook for %s", ticker)
            return

        yes_asks = book.get("yes", [])
        no_asks = book.get("no", [])

        if not yes_asks or not no_asks:
            return

        # Orderbook returns [[price, quantity], ...] sorted best first
        best_yes_ask = yes_asks[0][0]
        best_no_ask = no_asks[0][0]
        combined = best_yes_ask + best_no_ask

        if combined <= config.ARB_THRESHOLD:
            profit_cents = 100 - combined
            logger.info(
                "ARB DETECTED on %s: Yes=%d + No=%d = %d (profit=%d cents/contract)",
                ticker, best_yes_ask, best_no_ask, combined, profit_cents,
            )
            self._execute_arb(ticker, best_yes_ask, best_no_ask)
        else:
            logger.debug(
                "%s: Yes=%d + No=%d = %d (no arb)", ticker, best_yes_ask, best_no_ask, combined
            )

    def _execute_arb(self, ticker: str, yes_price: int, no_price: int):
        """Place both legs and monitor fills."""
        # Block this ticker immediately to prevent duplicates (Fix #1)
        self.traded_tickers.add(ticker)

        count = config.MAX_CONTRACTS
        logger.info("Placing arb on %s: %d contracts Yes@%d + No@%d", ticker, count, yes_price, no_price)

        try:
            yes_order = kalshi_api.create_order(ticker, "yes", yes_price, count)
            no_order = kalshi_api.create_order(ticker, "no", no_price, count)
        except Exception:
            logger.exception("Failed to place orders for %s", ticker)
            # If we failed to place, allow retry
            self.traded_tickers.discard(ticker)
            return

        yes_id = yes_order.get("order_id", "")
        no_id = no_order.get("order_id", "")
        logger.info("Orders placed — Yes: %s, No: %s", yes_id, no_id)

        # Start fill monitoring in a background thread so the main loop isn't blocked
        thread = threading.Thread(
            target=self._monitor_fills,
            args=(ticker, yes_id, no_id, yes_price, no_price),
            daemon=True,
        )
        thread.start()

    def _monitor_fills(self, ticker: str, yes_id: str, no_id: str, yes_price: int, no_price: int):
        """Monitor fill status and handle partial fills (Fix #3)."""
        # Wait before first check
        time.sleep(config.FILL_CHECK_DELAY)

        yes_order = kalshi_api.get_order(yes_id)
        no_order = kalshi_api.get_order(no_id)

        yes_filled = yes_order.get("status") == "filled" or yes_order.get("remaining_count", 1) == 0
        no_filled = no_order.get("status") == "filled" or no_order.get("remaining_count", 1) == 0

        if yes_filled and no_filled:
            profit = 100 - yes_price - no_price
            logger.info(
                "BOTH LEGS FILLED on %s — locked in %d cents/contract profit", ticker, profit
            )
            return

        if yes_filled and not no_filled:
            logger.warning("ONE-SIDED FILL on %s: Yes filled, No open — cancelling No", ticker)
            self._safe_cancel(no_id)
            logger.warning("UNHEDGED EXPOSURE on %s: holding Yes@%d with no hedge", ticker, yes_price)
            return

        if no_filled and not yes_filled:
            logger.warning("ONE-SIDED FILL on %s: No filled, Yes open — cancelling Yes", ticker)
            self._safe_cancel(yes_id)
            logger.warning("UNHEDGED EXPOSURE on %s: holding No@%d with no hedge", ticker, no_price)
            return

        # Neither filled — wait until safety deadline then cancel both
        remaining_wait = config.SAFETY_CANCEL_DELAY - config.FILL_CHECK_DELAY
        if remaining_wait > 0:
            time.sleep(remaining_wait)

        # Re-check before cancelling
        yes_order = kalshi_api.get_order(yes_id)
        no_order = kalshi_api.get_order(no_id)

        yes_filled = yes_order.get("status") == "filled" or yes_order.get("remaining_count", 1) == 0
        no_filled = no_order.get("status") == "filled" or no_order.get("remaining_count", 1) == 0

        if yes_filled and no_filled:
            profit = 100 - yes_price - no_price
            logger.info("BOTH LEGS FILLED (late) on %s — %d cents/contract profit", ticker, profit)
            return

        if not yes_filled and not no_filled:
            logger.info("Neither leg filled on %s — cancelling both, allowing retry", ticker)
            self._safe_cancel(yes_id)
            self._safe_cancel(no_id)
            self.traded_tickers.discard(ticker)  # Allow retry since nothing filled
            return

        # One filled during the extra wait — cancel the other
        if yes_filled:
            self._safe_cancel(no_id)
            logger.warning("UNHEDGED EXPOSURE on %s: Yes filled, No cancelled at safety deadline", ticker)
        else:
            self._safe_cancel(yes_id)
            logger.warning("UNHEDGED EXPOSURE on %s: No filled, Yes cancelled at safety deadline", ticker)

    def _safe_cancel(self, order_id: str):
        """Cancel an order, ignoring errors if already filled/cancelled."""
        try:
            kalshi_api.cancel_order(order_id)
            logger.info("Cancelled order %s", order_id)
        except Exception:
            logger.debug("Could not cancel order %s (may already be filled/cancelled)", order_id)

    def _check_exposure(self) -> bool:
        """Check if current exposure is under the limit (Fix #4)."""
        try:
            positions = kalshi_api.get_positions()
            total_exposure = 0
            for pos in positions:
                # Each position's cost is roughly contracts * avg price
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
            return True  # Allow trading if we can't check (fail open)
