"""Lightweight logging functions for the bot to call.

Each function inserts one or more rows into the analytics DB.
Wire these into bot.py at the appropriate decision points.
"""

import time
import db


def init_db():
    """Create tables if they don't exist. Call once at bot startup."""
    db.init_db()


def get_table_counts():
    """Return row counts for each table — for verification."""
    conn = db.get_connection(readonly=True)
    try:
        counts = {}
        for table in ("scans", "ladder_snapshots", "opportunities", "trades"):
            row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
            counts[table] = row[0]
        return counts
    finally:
        conn.close()


def log_scan(expiry_window, num_strikes, scan_duration_ms=None, series_ticker=""):
    """Log a completed scan cycle for one expiry window."""
    conn = db.get_connection()
    try:
        conn.execute(
            "INSERT INTO scans (timestamp, series_ticker, expiry_window, num_strikes, scan_duration_ms) "
            "VALUES (?, ?, ?, ?, ?)",
            (time.time(), series_ticker, expiry_window, num_strikes, scan_duration_ms),
        )
        conn.commit()
    finally:
        conn.close()


def log_snapshot(expiry_window, strikes, series_ticker=""):
    """Log a full ladder snapshot — one row per strike.

    strikes: list of scanner.StrikeLevel (or dicts with same keys)
    """
    now = time.time()
    conn = db.get_connection()
    try:
        rows = []
        for s in strikes:
            if hasattr(s, "strike"):
                rows.append((
                    now, series_ticker, expiry_window, s.strike,
                    s.yes_ask, s.yes_bid, s.no_ask, s.no_bid,
                    s.yes_ask_depth, s.no_ask_depth,
                ))
            else:
                rows.append((
                    now, series_ticker, expiry_window, s["strike"],
                    s["yes_ask"], s["yes_bid"], s["no_ask"], s["no_bid"],
                    s["yes_ask_depth"], s["no_ask_depth"],
                ))
        conn.executemany(
            "INSERT INTO ladder_snapshots "
            "(timestamp, series_ticker, expiry_window, strike, yes_ask, yes_bid, no_ask, no_bid, yes_depth, no_depth) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()


def log_opportunity(expiry_window, opp_type, sub_type, strike_low, strike_high,
                    yes_ask_low, no_ask_high, combined_cost, estimated_profit,
                    estimated_profit_maker=None, series_ticker="",
                    btc_price_at_detection=None, time_to_expiry_seconds=None,
                    depth_thin_side=None):
    """Log a detected arbitrage opportunity."""
    conn = db.get_connection()
    try:
        conn.execute(
            "INSERT INTO opportunities "
            "(timestamp, series_ticker, expiry_window, opp_type, sub_type, strike_low, strike_high, "
            "yes_ask_low, no_ask_high, combined_cost, estimated_profit, "
            "estimated_profit_maker, "
            "btc_price_at_detection, time_to_expiry_seconds, depth_thin_side) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (time.time(), series_ticker, expiry_window, opp_type, sub_type, strike_low, strike_high,
             yes_ask_low, no_ask_high, combined_cost, estimated_profit,
             estimated_profit_maker,
             btc_price_at_detection, time_to_expiry_seconds, depth_thin_side),
        )
        conn.commit()
    finally:
        conn.close()


def get_maker_summary(window_seconds=1800):
    """Return 30-minute summary of hard arb opportunities with maker fee analysis.

    Returns list of dicts (one per series + an 'ALL' aggregate), each with keys:
    series, total_hard_arbs, profitable_taker, profitable_maker,
    avg_gross_spread_maker, avg_depth_maker. Returns None if no data.
    """
    conn = db.get_connection(readonly=True)
    try:
        cutoff = time.time() - window_seconds
        rows = conn.execute(
            "SELECT series_ticker, combined_cost, estimated_profit, estimated_profit_maker, depth_thin_side "
            "FROM opportunities "
            "WHERE opp_type = 'C' AND sub_type = 'hard' AND timestamp >= ?",
            (cutoff,),
        ).fetchall()

        if not rows:
            return None

        def _summarize(subset):
            total = len(subset)
            profitable_taker = sum(1 for r in subset if r["estimated_profit"] > 0)
            profitable_maker = sum(1 for r in subset if r["estimated_profit_maker"] is not None and r["estimated_profit_maker"] > 0)
            maker_rows = [r for r in subset if r["estimated_profit_maker"] is not None and r["estimated_profit_maker"] > 0]
            if maker_rows:
                avg_spread = sum(100 - r["combined_cost"] for r in maker_rows) / len(maker_rows)
                depths = [r["depth_thin_side"] for r in maker_rows if r["depth_thin_side"] is not None]
                avg_depth = sum(depths) / len(depths) if depths else 0
            else:
                avg_spread = 0
                avg_depth = 0
            return {
                "total_hard_arbs": total,
                "profitable_taker": profitable_taker,
                "profitable_maker": profitable_maker,
                "avg_gross_spread_maker": avg_spread,
                "avg_depth_maker": avg_depth,
            }

        # Aggregate across all series
        result = _summarize(rows)
        result["series"] = "ALL"

        # Per-series breakdown
        by_series = {}
        for r in rows:
            s = r["series_ticker"] or "UNKNOWN"
            by_series.setdefault(s, []).append(r)

        per_series = []
        for s, subset in sorted(by_series.items()):
            d = _summarize(subset)
            d["series"] = s
            per_series.append(d)

        return {"all": result, "per_series": per_series}
    finally:
        conn.close()


def update_arb_stability(expiry_window, current_arbs, series_ticker=""):
    """Track arb persistence. Call each scan with the set of currently-visible arbs.

    current_arbs: list of dicts with keys: strike_low, strike_high, combined_cost, depth_thin_side
    """
    now = time.time()
    conn = db.get_connection()
    try:
        # Get all open arbs for this expiry
        open_rows = conn.execute(
            "SELECT id, strike_low, strike_high FROM arb_stability "
            "WHERE expiry_window = ? AND series_ticker = ? AND status = 'open'",
            (expiry_window, series_ticker),
        ).fetchall()

        open_map = {}
        for r in open_rows:
            key = (r["strike_low"], r["strike_high"])
            open_map[key] = r["id"]

        # Current arb keys
        current_keys = set()
        for arb in current_arbs:
            key = (arb["strike_low"], arb["strike_high"])
            current_keys.add(key)

            if key in open_map:
                # Update existing: increment scan_count, update cost/depth
                conn.execute(
                    "UPDATE arb_stability SET timestamp = ?, scan_count = scan_count + 1, "
                    "combined_cost = ?, depth_thin_side = ? WHERE id = ?",
                    (now, arb["combined_cost"], arb.get("depth_thin_side"), open_map[key]),
                )
            else:
                # New arb
                conn.execute(
                    "INSERT INTO arb_stability "
                    "(timestamp, series_ticker, expiry_window, strike_low, strike_high, "
                    "combined_cost, depth_thin_side, first_seen, scan_count, status) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 'open')",
                    (now, series_ticker, expiry_window, arb["strike_low"], arb["strike_high"],
                     arb["combined_cost"], arb.get("depth_thin_side"), now),
                )

        # Close arbs that are no longer visible
        for key, row_id in open_map.items():
            if key not in current_keys:
                conn.execute(
                    "UPDATE arb_stability SET status = 'closed', close_reason = 'spread_closed', "
                    "timestamp = ? WHERE id = ?",
                    (now, row_id),
                )

        conn.commit()
    finally:
        conn.close()


def get_stability_summary(window_seconds=1800):
    """Return stability summary for recently closed arbs."""
    conn = db.get_connection(readonly=True)
    try:
        cutoff = time.time() - window_seconds
        rows = conn.execute(
            "SELECT series_ticker, scan_count, "
            "timestamp - first_seen AS duration_seconds, close_reason "
            "FROM arb_stability WHERE timestamp >= ? AND status = 'closed'",
            (cutoff,),
        ).fetchall()

        if not rows:
            return None

        total = len(rows)
        avg_scans = sum(r["scan_count"] for r in rows) / total
        avg_duration = sum(r["duration_seconds"] for r in rows) / total
        return {
            "total_closed": total,
            "avg_scan_count": avg_scans,
            "avg_duration_seconds": avg_duration,
        }
    finally:
        conn.close()


def log_binary_arb_trade(ticker, yes_price, no_price, combined_cost, size,
                         yes_order_id=None, no_order_id=None):
    """Log a binary arb trade attempt. Returns the row id for later updates."""
    conn = db.get_connection()
    try:
        cursor = conn.execute(
            "INSERT INTO binary_arb_trades "
            "(timestamp, ticker, yes_price, no_price, combined_cost, size, "
            "yes_order_id, no_order_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (time.time(), ticker, yes_price, no_price, combined_cost, size,
             yes_order_id, no_order_id),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def update_binary_arb_trade(row_id, yes_filled=0, no_filled=0,
                            hedge_action=None, realized_pnl=None, fees=None):
    """Update a binary arb trade with fill/hedge results."""
    conn = db.get_connection()
    try:
        conn.execute(
            "UPDATE binary_arb_trades SET "
            "yes_filled = ?, no_filled = ?, hedge_action = ?, "
            "realized_pnl = ?, fees = ? WHERE id = ?",
            (yes_filled, no_filled, hedge_action, realized_pnl, fees, row_id),
        )
        conn.commit()
    finally:
        conn.close()


def log_paper_trade(event_ticker, series_ticker, event_title,
                    bucket_ticker, bucket_label, category, signal_type,
                    entry_price, fair_value_est, overpricing_gap,
                    total_event_excess, yes_depth=0,
                    yes_bid=None, yes_ask=None, spread=None, bid_depth=None):
    """Log a hypothetical SELL YES paper trade."""
    conn = db.get_connection()
    try:
        conn.execute(
            "INSERT INTO paper_trades "
            "(timestamp, event_ticker, series_ticker, event_title, "
            "bucket_ticker, bucket_label, category, signal_type, "
            "entry_price, fair_value_est, overpricing_gap, "
            "total_event_excess, yes_depth, "
            "yes_bid, yes_ask, spread, bid_depth) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (time.time(), event_ticker, series_ticker, event_title,
             bucket_ticker, bucket_label, category, signal_type,
             entry_price, fair_value_est, overpricing_gap,
             total_event_excess, yes_depth,
             yes_bid, yes_ask, spread, bid_depth),
        )
        conn.commit()
    finally:
        conn.close()


def log_paper_near_miss(event_ticker, series_ticker, bucket_ticker,
                        bucket_label, category, signal_type,
                        yes_price, fair_value_est, gap, threshold_used):
    """Log a bucket that was close to the signal threshold but didn't fire."""
    conn = db.get_connection()
    try:
        conn.execute(
            "INSERT INTO paper_near_misses "
            "(timestamp, event_ticker, series_ticker, bucket_ticker, "
            "bucket_label, category, signal_type, "
            "yes_price, fair_value_est, gap, threshold_used) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (time.time(), event_ticker, series_ticker, bucket_ticker,
             bucket_label, category, signal_type,
             yes_price, fair_value_est, gap, threshold_used),
        )
        conn.commit()
    finally:
        conn.close()


def resolve_paper_trade(trade_id, resolved_price, pnl_cents, adjusted_pnl_cents=None):
    """Mark a paper trade as resolved with outcome and P&L."""
    conn = db.get_connection()
    try:
        conn.execute(
            "UPDATE paper_trades SET status = 'resolved', "
            "resolved_price = ?, pnl_cents = ?, adjusted_pnl_cents = ?, resolved_at = ? "
            "WHERE id = ?",
            (resolved_price, pnl_cents, adjusted_pnl_cents, time.time(), trade_id),
        )
        conn.commit()
    finally:
        conn.close()


def get_open_paper_trades():
    """Return all open (unresolved) paper trades."""
    conn = db.get_connection(readonly=True)
    try:
        rows = conn.execute(
            "SELECT id, event_ticker, series_ticker, bucket_ticker, "
            "entry_price, category, signal_type, yes_bid "
            "FROM paper_trades WHERE status = 'open'"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def has_open_or_recent_paper_trade(bucket_ticker, cooldown_seconds=86400):
    """Check if a bucket already has an open trade OR was traded within cooldown.

    Returns True if we should SKIP logging a new paper trade for this ticker.
    Dedup rules:
    1. If there's an open (unresolved) trade for this ticker → skip
    2. If the most recent trade (open or resolved) was within cooldown_seconds → skip
    """
    conn = db.get_connection(readonly=True)
    try:
        # Check for any open trade on this ticker
        open_row = conn.execute(
            "SELECT 1 FROM paper_trades WHERE bucket_ticker = ? AND status = 'open' LIMIT 1",
            (bucket_ticker,),
        ).fetchone()
        if open_row:
            return True

        # Check cooldown: most recent trade (any status) within window
        cutoff = time.time() - cooldown_seconds
        recent_row = conn.execute(
            "SELECT 1 FROM paper_trades WHERE bucket_ticker = ? AND timestamp >= ? LIMIT 1",
            (bucket_ticker, cutoff),
        ).fetchone()
        return recent_row is not None
    finally:
        conn.close()


def log_mispricing_signal(event_ticker, series_ticker, event_title,
                          bucket_ticker, bucket_label, category,
                          current_price, fair_value_est, overpricing_gap,
                          total_event_excess, yes_depth=0,
                          yes_bid=None, yes_ask=None, spread=None, bid_depth=None):
    """Log a detected mispricing signal (overpriced YES bucket)."""
    conn = db.get_connection()
    try:
        conn.execute(
            "INSERT INTO mispricing_signals "
            "(timestamp, event_ticker, series_ticker, event_title, "
            "bucket_ticker, bucket_label, category, "
            "current_price, fair_value_est, overpricing_gap, "
            "total_event_excess, yes_depth, "
            "yes_bid, yes_ask, spread, bid_depth) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (time.time(), event_ticker, series_ticker, event_title,
             bucket_ticker, bucket_label, category,
             current_price, fair_value_est, overpricing_gap,
             total_event_excess, yes_depth,
             yes_bid, yes_ask, spread, bid_depth),
        )
        conn.commit()
    finally:
        conn.close()


def log_live_order(paper_trade_id, order_id, bucket_ticker, price_cents, count, expires_at,
                   side="yes", action="sell"):
    """Log a live order placed on Kalshi."""
    conn = db.get_connection()
    try:
        cursor = conn.execute(
            "INSERT INTO live_orders "
            "(timestamp, paper_trade_id, order_id, bucket_ticker, side, action, "
            "price_cents, count, status, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)",
            (time.time(), paper_trade_id, order_id, bucket_ticker, side, action,
             price_cents, count, expires_at),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def update_live_order(order_db_id, status=None, filled_count=None, filled_price=None, cancelled_at=None):
    """Update a live order's status/fill info."""
    conn = db.get_connection()
    try:
        updates = []
        params = []
        if status is not None:
            updates.append("status = ?")
            params.append(status)
        if filled_count is not None:
            updates.append("filled_count = ?")
            params.append(filled_count)
        if filled_price is not None:
            updates.append("filled_price = ?")
            params.append(filled_price)
        if cancelled_at is not None:
            updates.append("cancelled_at = ?")
            params.append(cancelled_at)
        if not updates:
            return
        params.append(order_db_id)
        conn.execute(
            f"UPDATE live_orders SET {', '.join(updates)} WHERE id = ?",
            params,
        )
        conn.commit()
    finally:
        conn.close()


def get_open_live_orders():
    """Return all open live orders."""
    conn = db.get_connection(readonly=True)
    try:
        rows = conn.execute(
            "SELECT id, order_id, bucket_ticker, price_cents, count, expires_at "
            "FROM live_orders WHERE status = 'open'"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def log_trade(expiry_window, opp_type, strike_low, strike_high,
              leg1_side, leg1_price, leg1_fill_status,
              leg2_side=None, leg2_price=None, leg2_fill_status=None,
              orphaned=False, exit_price=None, realized_pnl=None, fees=None):
    """Log an executed (or attempted) trade."""
    conn = db.get_connection()
    try:
        conn.execute(
            "INSERT INTO trades "
            "(timestamp, expiry_window, opp_type, strike_low, strike_high, "
            "leg1_side, leg1_price, leg1_fill_status, "
            "leg2_side, leg2_price, leg2_fill_status, "
            "orphaned, exit_price, realized_pnl, fees) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (time.time(), expiry_window, opp_type, strike_low, strike_high,
             leg1_side, leg1_price, leg1_fill_status,
             leg2_side, leg2_price, leg2_fill_status,
             int(orphaned), exit_price, realized_pnl, fees),
        )
        conn.commit()
    finally:
        conn.close()
