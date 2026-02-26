"""Market-making engine for Kalshi above/below contracts.

Runs as an alternative mode alongside the arb scanner.
Quotes bid/ask on ATM strikes, captures spread P&L.
"""

import logging
import signal
import time
import uuid
from collections import deque
from dataclasses import dataclass, field

import config
import mm_config as mc
import mm_logger
from kalshi_api import (
    cancel_order,
    create_order,
    get_balance,
    get_markets,
    get_open_orders,
    get_order,
    get_orderbook,
    get_positions,
)

logger = logging.getLogger("mm-engine")


@dataclass
class Fill:
    side: str       # "yes" or "no"
    price: int      # cents paid
    count: int
    timestamp: float


@dataclass
class StrikeState:
    ticker: str
    strike: float
    bid_order_id: str = ""
    ask_order_id: str = ""
    bid_price: int = 0
    ask_price: int = 0
    bid_last_remaining: int = 0  # track remaining_count to detect fill deltas
    ask_last_remaining: int = 0
    inventory: int = 0           # positive = long yes, negative = long no
    yes_fills: deque = field(default_factory=deque)
    no_fills: deque = field(default_factory=deque)
    realized_pnl: float = 0.0   # cents


class MarketMaker:
    def __init__(self):
        self.running = False
        self._stopped = False
        self.cycle_count = 0
        self.consecutive_errors = 0
        self.strikes: dict[str, StrikeState] = {}   # ticker -> StrikeState
        self.halted = False
        self.last_strike_refresh = 0.0
        self.last_summary_time = 0.0
        self._starting_balance = 0

        # Pull fee config from config.SERIES for this series
        series_cfg = config.SERIES.get(mc.MM_SERIES, {})
        self.taker_fee = series_cfg.get("taker_fee", 0.07)
        self.maker_mult = series_cfg.get("maker_mult", 0.0175)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self):
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

        self._startup_checks()
        self.running = True
        logger.info("Market maker started — dry_run=%s", not mc.MM_CONFIRM)

        while self.running and not self.halted:
            try:
                self._cycle()
                self.consecutive_errors = 0
            except Exception:
                self.consecutive_errors += 1
                logger.exception("Cycle error (%d/%d)",
                                 self.consecutive_errors, mc.MM_MAX_API_ERRORS)
                if self.consecutive_errors >= mc.MM_MAX_API_ERRORS:
                    logger.error("Too many consecutive errors — halting")
                    self.halted = True
                    break

            time.sleep(mc.MM_REQUOTE_INTERVAL)

        self.stop()

    def stop(self):
        if self._stopped:
            return
        self._stopped = True
        self.running = False
        self._cancel_all()
        self._log_final_state()
        logger.info("Market maker stopped")

    def _signal_handler(self, signum, frame):
        logger.info("Signal %s received — shutting down", signum)
        self.running = False

    # ------------------------------------------------------------------
    # Startup
    # ------------------------------------------------------------------

    def _startup_checks(self):
        try:
            self._starting_balance = get_balance()
            logger.info("Starting balance: $%.2f", self._starting_balance / 100)
        except Exception:
            logger.exception("Could not fetch balance")

        try:
            positions = get_positions()
            if positions:
                tickers = [p.get("ticker", "?") for p in positions]
                logger.warning("Existing positions detected: %s", tickers)
        except Exception:
            logger.exception("Could not fetch positions")

        # Cancel any stale orders from a previous session
        try:
            open_orders = get_open_orders()
            if open_orders:
                logger.warning("Found %d stale open orders — cancelling", len(open_orders))
                for order in open_orders:
                    oid = order.get("order_id", "")
                    ticker = order.get("ticker", "?")
                    try:
                        cancel_order(oid)
                        logger.info("Cancelled stale order %s on %s", oid, ticker)
                    except Exception:
                        logger.exception("Failed to cancel stale order %s", oid)
            else:
                logger.info("No stale open orders")
        except Exception:
            logger.exception("Could not fetch open orders")

    # ------------------------------------------------------------------
    # Main cycle
    # ------------------------------------------------------------------

    def _cycle(self):
        self.cycle_count += 1
        now = time.time()

        # Refresh ATM strikes every 5 minutes
        if now - self.last_strike_refresh > 300:
            self._select_strikes()
            self.last_strike_refresh = now

        if not self.strikes:
            logger.warning("No strikes selected — skipping cycle")
            return

        for ticker, st in list(self.strikes.items()):
            try:
                self._process_strike(st)
            except Exception:
                logger.exception("Error processing %s", ticker)

        # Circuit breaker: max loss
        total_rpnl = sum(s.realized_pnl for s in self.strikes.values())
        if total_rpnl < -mc.MM_MAX_LOSS:
            logger.error("MAX_LOSS breaker: rpnl=%.0fc — halting", total_rpnl)
            self.halted = True
            return

        # Log snapshot
        mm_logger.log_snapshot(self.cycle_count, self.strikes, total_rpnl)

        # Stdout summary line
        for ticker, st in self.strikes.items():
            status = "HALTED" if self.halted else "QUOTING"
            if abs(st.inventory) >= mc.MM_MAX_INVENTORY:
                status = "INV_LIMIT"
            upnl = self._estimate_upnl(st)
            print(f"[MM] {mc.MM_SERIES} {st.strike}: "
                  f"bid={st.bid_price} ask={st.ask_price} "
                  f"inv={st.inventory:+d} rpnl={st.realized_pnl:+.0f}c "
                  f"upnl={upnl:+.0f}c [{status}]")

        # 30-minute summary
        if now - self.last_summary_time > 1800:
            self._print_summary()
            self.last_summary_time = now

    # ------------------------------------------------------------------
    # Strike selection
    # ------------------------------------------------------------------

    def _select_strikes(self):
        markets = get_markets(mc.MM_SERIES)
        if not markets:
            logger.warning("No markets found for %s", mc.MM_SERIES)
            return

        now = time.time()

        # Filter by TTL
        valid = []
        for m in markets:
            close_time = m.get("close_time") or m.get("expiration_time", "")
            if not close_time:
                continue
            # Parse ISO timestamp
            try:
                from datetime import datetime, timezone
                if isinstance(close_time, str):
                    ct = close_time.replace("Z", "+00:00")
                    exp_ts = datetime.fromisoformat(ct).timestamp()
                else:
                    exp_ts = float(close_time)
            except (ValueError, TypeError):
                continue

            ttl = exp_ts - now
            if mc.MM_MIN_EXPIRY <= ttl <= mc.MM_MAX_EXPIRY:
                m["_ttl"] = ttl
                m["_exp_ts"] = exp_ts
                valid.append(m)

        if not valid:
            logger.warning("No markets in TTL window [%d, %d]s",
                           mc.MM_MIN_EXPIRY, mc.MM_MAX_EXPIRY)
            return

        # Group by event (expiry window)
        by_event = {}
        for m in valid:
            evt = m.get("event_ticker", "")
            by_event.setdefault(evt, []).append(m)

        # Pick event with TTL closest to midpoint
        mid_ttl = (mc.MM_MIN_EXPIRY + mc.MM_MAX_EXPIRY) / 2
        best_event = min(by_event.keys(),
                         key=lambda e: abs(by_event[e][0]["_ttl"] - mid_ttl))
        candidates = by_event[best_event]

        # Extract strike values and sort
        for m in candidates:
            # Strike is typically in the ticker or subtitle
            strike_val = self._parse_strike(m)
            m["_strike"] = strike_val

        candidates = [m for m in candidates if m["_strike"] is not None]
        candidates.sort(key=lambda m: m["_strike"])

        if not candidates:
            logger.warning("Could not parse strikes for %s", best_event)
            return

        # Find ATM: probe middle strikes, pick yes_ask nearest 50c
        mid_idx = len(candidates) // 2
        start = max(0, mid_idx - 5)
        end = min(len(candidates), mid_idx + 5)
        probe = candidates[start:end]

        best_atm = None
        best_dist = float("inf")
        for m in probe:
            ticker = m.get("ticker", "")
            try:
                book = get_orderbook(ticker, depth=1)
                yes_levels = book.get("yes", [])
                if yes_levels:
                    # Best yes bid = highest price in the yes buy book
                    best_yb = max(l[0] for l in yes_levels if isinstance(l, list))
                else:
                    best_yb = 50
                dist = abs(best_yb - 50)
                if dist < best_dist:
                    best_dist = dist
                    best_atm = m
            except Exception:
                continue

        if best_atm is None:
            best_atm = candidates[len(candidates) // 2]

        atm_idx = candidates.index(best_atm)

        # Select ATM + 1 above + 1 below
        selected = []
        if atm_idx > 0:
            selected.append(candidates[atm_idx - 1])
        selected.append(best_atm)
        if atm_idx < len(candidates) - 1:
            selected.append(candidates[atm_idx + 1])

        # Build new strikes dict, keeping existing state if ticker matches
        new_strikes = {}
        for m in selected:
            ticker = m.get("ticker", "")
            strike = m["_strike"]
            if ticker in self.strikes:
                new_strikes[ticker] = self.strikes[ticker]
            else:
                new_strikes[ticker] = StrikeState(ticker=ticker, strike=strike)

        # Keep old strikes that have inventory (don't abandon positions)
        for ticker, st in self.strikes.items():
            if ticker not in new_strikes and st.inventory != 0:
                new_strikes[ticker] = st
                logger.info("Keeping %s (inv=%d) despite ATM shift", ticker, st.inventory)

        old_tickers = set(self.strikes.keys())
        new_tickers = set(new_strikes.keys())
        if new_tickers != old_tickers:
            added = new_tickers - old_tickers
            removed = old_tickers - new_tickers
            if added:
                logger.info("Added strikes: %s", added)
            if removed:
                logger.info("Removed strikes: %s", removed)

        self.strikes = new_strikes

    def _parse_strike(self, market):
        """Extract numeric strike from market data."""
        # Try floor_strike or custom_strike fields first
        for key in ("floor_strike", "custom_strike", "strike"):
            val = market.get(key)
            if val is not None:
                try:
                    return float(val)
                except (ValueError, TypeError):
                    pass

        # Try parsing from subtitle like "Bitcoin above 68000?"
        subtitle = market.get("subtitle", "") or market.get("title", "")
        import re
        match = re.search(r"[\d,]+\.?\d*", subtitle.replace(",", ""))
        if match:
            try:
                return float(match.group())
            except ValueError:
                pass

        return None

    # ------------------------------------------------------------------
    # Per-strike processing
    # ------------------------------------------------------------------

    def _process_strike(self, st: StrikeState):
        # 1. Fetch orderbook
        book = get_orderbook(st.ticker, depth=3)

        # 2. Check fills on existing orders
        self._check_fills(st)

        # 3. Compute quotes
        bid_target, ask_target = self._compute_quotes(st, book)

        # 4. Manage orders
        self._manage_orders(st, bid_target, ask_target)

    def _compute_quotes(self, st: StrikeState, book: dict):
        """Compute target bid/ask prices from orderbook.

        Kalshi orderbook: 'yes' and 'no' arrays each contain BUY orders
        (bids) as [[price, qty], ...].
        - best_yes_bid = highest yes buy price
        - best_no_bid = highest no buy price
        - derived yes_ask = 100 - best_no_bid (selling yes = buying no)
        - native spread = derived_yes_ask - best_yes_bid
        """
        yes_levels = book.get("yes", [])
        no_levels = book.get("no", [])

        best_yes_bid = self._best_bid(yes_levels)
        best_no_bid = self._best_bid(no_levels)

        if best_yes_bid is None or best_no_bid is None:
            return None, None

        derived_yes_ask = 100 - best_no_bid
        native_spread = derived_yes_ask - best_yes_bid

        if native_spread < mc.MM_MIN_BOOK_SPREAD:
            return None, None

        # Mid from best yes bid and derived yes ask
        mid = (best_yes_bid + derived_yes_ask) / 2

        # Inventory skew: push mid away from inventory
        adjusted_mid = mid - (st.inventory * 1)

        # Target prices
        target_bid = int(adjusted_mid - mc.MM_HALF_SPREAD)
        target_ask = int(adjusted_mid + mc.MM_HALF_SPREAD)

        # Safety clamps: don't cross the book
        target_bid = min(target_bid, derived_yes_ask - 1)  # bid below best ask
        target_ask = max(target_ask, best_yes_bid + 1)     # ask above best bid
        target_bid = max(1, target_bid)
        target_ask = min(99, target_ask)
        if target_bid >= target_ask:
            return None, None

        return target_bid, target_ask

    def _best_bid(self, levels):
        """Extract highest bid price from orderbook levels [[price, qty], ...]."""
        if not levels:
            return None
        prices = []
        for level in levels:
            if isinstance(level, list):
                prices.append(level[0])
            elif isinstance(level, dict):
                prices.append(level.get("price", 0))
        return max(prices) if prices else None

    # ------------------------------------------------------------------
    # Order management
    # ------------------------------------------------------------------

    def _manage_orders(self, st: StrikeState, bid_target, ask_target):
        # Handle bid side
        if bid_target is not None and st.inventory < mc.MM_MAX_INVENTORY:
            if st.bid_order_id and abs(st.bid_price - bid_target) <= mc.MM_QUOTE_TOLERANCE:
                pass  # leave existing order
            else:
                self._cancel_if_active(st, "bid")
                self._place_bid(st, bid_target)
        else:
            # Pull bid: inventory at limit or no valid quote
            self._cancel_if_active(st, "bid")
            st.bid_price = 0

        # Handle ask side
        if ask_target is not None and st.inventory > -mc.MM_MAX_INVENTORY:
            if st.ask_order_id and abs(st.ask_price - ask_target) <= mc.MM_QUOTE_TOLERANCE:
                pass  # leave existing order
            else:
                self._cancel_if_active(st, "ask")
                self._place_ask(st, ask_target)
        else:
            self._cancel_if_active(st, "ask")
            st.ask_price = 0

    def _place_bid(self, st: StrikeState, price: int):
        """Place bid = buy yes at price."""
        st.bid_price = price
        st.bid_last_remaining = mc.MM_QUOTE_SIZE
        mm_logger.log_quote(st.ticker, "bid", price, mc.MM_QUOTE_SIZE, "place")

        if not mc.MM_CONFIRM:
            st.bid_order_id = f"DRY-BID-{uuid.uuid4().hex[:8]}"
            return

        try:
            order = create_order(st.ticker, "yes", price, mc.MM_QUOTE_SIZE,
                                 post_only=True)
            st.bid_order_id = order.get("order_id", "")
        except Exception:
            logger.exception("Failed to place bid on %s at %d", st.ticker, price)
            st.bid_order_id = ""
            st.bid_last_remaining = 0

    def _place_ask(self, st: StrikeState, price: int):
        """Place ask = buy no at (100 - price) to sell yes at price."""
        st.ask_price = price
        st.ask_last_remaining = mc.MM_QUOTE_SIZE
        no_price = 100 - price
        mm_logger.log_quote(st.ticker, "ask", price, mc.MM_QUOTE_SIZE, "place")

        if not mc.MM_CONFIRM:
            st.ask_order_id = f"DRY-ASK-{uuid.uuid4().hex[:8]}"
            return

        try:
            order = create_order(st.ticker, "no", no_price, mc.MM_QUOTE_SIZE,
                                 post_only=True)
            st.ask_order_id = order.get("order_id", "")
        except Exception:
            logger.exception("Failed to place ask on %s at %d (no@%d)",
                             st.ticker, price, no_price)
            st.ask_order_id = ""
            st.ask_last_remaining = 0

    def _cancel_if_active(self, st: StrikeState, side: str):
        """Cancel an existing order if it's active."""
        order_id = st.bid_order_id if side == "bid" else st.ask_order_id
        if not order_id or order_id.startswith("DRY-"):
            # Clear dry-run or empty order
            if side == "bid":
                st.bid_order_id = ""
                st.bid_last_remaining = 0
            else:
                st.ask_order_id = ""
                st.ask_last_remaining = 0
            return

        try:
            cancel_order(order_id)
            mm_logger.log_quote(st.ticker, side,
                                st.bid_price if side == "bid" else st.ask_price,
                                mc.MM_QUOTE_SIZE, "cancel")
        except Exception:
            logger.debug("Cancel failed for %s (may already be filled/cancelled)", order_id)

        if side == "bid":
            st.bid_order_id = ""
            st.bid_last_remaining = 0
        else:
            st.ask_order_id = ""
            st.ask_last_remaining = 0

    # ------------------------------------------------------------------
    # Fill detection
    # ------------------------------------------------------------------

    def _check_fills(self, st: StrikeState):
        """Check resting orders for fills via get_order."""
        self._check_order_fill(st, "bid")
        self._check_order_fill(st, "ask")

    def _check_order_fill(self, st: StrikeState, side: str):
        order_id = st.bid_order_id if side == "bid" else st.ask_order_id
        if not order_id or order_id.startswith("DRY-"):
            return

        try:
            order_data = get_order(order_id)
        except Exception:
            logger.debug("Could not fetch order %s", order_id)
            return

        status = order_data.get("status", "")
        remaining = order_data.get("remaining_count", 0)

        # Compare against last known remaining to get the delta (new fills only)
        last_remaining = st.bid_last_remaining if side == "bid" else st.ask_last_remaining
        new_fills = last_remaining - remaining

        if new_fills > 0:
            # Update last_remaining to current
            if side == "bid":
                st.bid_last_remaining = remaining
            else:
                st.ask_last_remaining = remaining

            price = st.bid_price if side == "bid" else st.ask_price
            fill_side = "yes" if side == "bid" else "no"

            fill = Fill(
                side=fill_side,
                price=price if side == "bid" else (100 - price),
                count=new_fills,
                timestamp=time.time(),
            )

            if side == "bid":
                st.inventory += new_fills
                st.yes_fills.append(fill)
            else:
                st.inventory -= new_fills
                st.no_fills.append(fill)

            # Match FIFO
            self._match_fifo(st)

            # Log fill
            total_rpnl = sum(s.realized_pnl for s in self.strikes.values())
            mm_logger.log_fill(st.ticker, fill_side, fill.price,
                               new_fills, st.inventory, total_rpnl)
            logger.info("FILL %s %s %dc x%d inv=%d rpnl=%.0fc",
                        st.ticker, fill_side, fill.price, new_fills,
                        st.inventory, st.realized_pnl)

        # If order is fully filled or cancelled, clear it and reset tracking
        if status in ("filled", "cancelled"):
            if side == "bid":
                st.bid_order_id = ""
                st.bid_last_remaining = 0
            else:
                st.ask_order_id = ""
                st.ask_last_remaining = 0

    # ------------------------------------------------------------------
    # FIFO P&L matching
    # ------------------------------------------------------------------

    def _match_fifo(self, st: StrikeState):
        """Match oldest yes fills against oldest no fills for realized P&L."""
        while st.yes_fills and st.no_fills:
            yes_fill = st.yes_fills[0]
            no_fill = st.no_fills[0]

            qty = min(yes_fill.count, no_fill.count)

            # Profit = payout (100c) - yes_price - no_price per contract
            gross = (100 - yes_fill.price - no_fill.price) * qty
            # Maker fees: maker_mult * P * (1-P) where P is in dollars (cents/100)
            yes_fee = self.maker_mult * yes_fill.price * (100 - yes_fill.price) / 100 * qty
            no_fee = self.maker_mult * no_fill.price * (100 - no_fill.price) / 100 * qty
            net = gross - yes_fee - no_fee

            st.realized_pnl += net

            # Reduce fill counts
            yes_fill.count -= qty
            no_fill.count -= qty
            if yes_fill.count == 0:
                st.yes_fills.popleft()
            if no_fill.count == 0:
                st.no_fills.popleft()

    # ------------------------------------------------------------------
    # Unrealized P&L estimate
    # ------------------------------------------------------------------

    def _estimate_upnl(self, st: StrikeState):
        """Rough unrealized P&L based on mid price assumption of 50c."""
        # Simplistic: assume each open contract can close at mid
        # Long yes (inv > 0): upnl = inv * (50 - avg_yes_price)
        # Long no (inv < 0): upnl = |inv| * (50 - avg_no_price)
        if st.inventory > 0 and st.yes_fills:
            avg = sum(f.price * f.count for f in st.yes_fills) / max(1, sum(f.count for f in st.yes_fills))
            return st.inventory * (50 - avg)
        elif st.inventory < 0 and st.no_fills:
            avg = sum(f.price * f.count for f in st.no_fills) / max(1, sum(f.count for f in st.no_fills))
            return abs(st.inventory) * (50 - avg)
        return 0.0

    # ------------------------------------------------------------------
    # Cancel all & shutdown
    # ------------------------------------------------------------------

    def _cancel_all(self):
        """Cancel all resting orders across all strikes."""
        for ticker, st in self.strikes.items():
            self._cancel_if_active(st, "bid")
            self._cancel_if_active(st, "ask")

    def _log_final_state(self):
        total_rpnl = sum(s.realized_pnl for s in self.strikes.values())
        print(f"\n{'='*50}")
        print(f"  Market Maker — Final State")
        print(f"{'='*50}")
        for ticker, st in self.strikes.items():
            upnl = self._estimate_upnl(st)
            print(f"  {ticker}: inv={st.inventory:+d} "
                  f"rpnl={st.realized_pnl:+.0f}c upnl={upnl:+.0f}c "
                  f"yes_q={len(st.yes_fills)} no_q={len(st.no_fills)}")
        print(f"  Total realized P&L: {total_rpnl:+.0f}c (${total_rpnl/100:+.2f})")
        print(f"  Cycles: {self.cycle_count}")
        print(f"{'='*50}\n")

    def _print_summary(self):
        total_rpnl = sum(s.realized_pnl for s in self.strikes.values())
        total_inv = sum(s.inventory for s in self.strikes.values())
        print(f"\n[MM SUMMARY] rpnl={total_rpnl:+.0f}c "
              f"inv={total_inv:+d} cycles={self.cycle_count} "
              f"strikes={len(self.strikes)}")
