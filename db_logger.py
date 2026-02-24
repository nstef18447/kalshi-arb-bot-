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


def log_scan(expiry_window, num_strikes, scan_duration_ms=None):
    """Log a completed scan cycle for one expiry window."""
    conn = db.get_connection()
    try:
        conn.execute(
            "INSERT INTO scans (timestamp, expiry_window, num_strikes, scan_duration_ms) "
            "VALUES (?, ?, ?, ?)",
            (time.time(), expiry_window, num_strikes, scan_duration_ms),
        )
        conn.commit()
    finally:
        conn.close()


def log_snapshot(expiry_window, strikes):
    """Log a full ladder snapshot — one row per strike.

    strikes: list of scanner.StrikeLevel (or dicts with same keys)
    """
    now = time.time()
    conn = db.get_connection()
    try:
        rows = []
        for s in strikes:
            if hasattr(s, "strike"):
                # StrikeLevel dataclass
                rows.append((
                    now, expiry_window, s.strike,
                    s.yes_ask, s.yes_bid, s.no_ask, s.no_bid,
                    s.yes_ask_depth, s.no_ask_depth,
                ))
            else:
                # dict fallback
                rows.append((
                    now, expiry_window, s["strike"],
                    s["yes_ask"], s["yes_bid"], s["no_ask"], s["no_bid"],
                    s["yes_ask_depth"], s["no_ask_depth"],
                ))
        conn.executemany(
            "INSERT INTO ladder_snapshots "
            "(timestamp, expiry_window, strike, yes_ask, yes_bid, no_ask, no_bid, yes_depth, no_depth) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()


def log_opportunity(expiry_window, opp_type, sub_type, strike_low, strike_high,
                    yes_ask_low, no_ask_high, combined_cost, estimated_profit,
                    btc_price_at_detection=None, time_to_expiry_seconds=None,
                    depth_thin_side=None):
    """Log a detected arbitrage opportunity."""
    conn = db.get_connection()
    try:
        conn.execute(
            "INSERT INTO opportunities "
            "(timestamp, expiry_window, opp_type, sub_type, strike_low, strike_high, "
            "yes_ask_low, no_ask_high, combined_cost, estimated_profit, "
            "btc_price_at_detection, time_to_expiry_seconds, depth_thin_side) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (time.time(), expiry_window, opp_type, sub_type, strike_low, strike_high,
             yes_ask_low, no_ask_high, combined_cost, estimated_profit,
             btc_price_at_detection, time_to_expiry_seconds, depth_thin_side),
        )
        conn.commit()
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
