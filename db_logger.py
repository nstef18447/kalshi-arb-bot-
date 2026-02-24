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
