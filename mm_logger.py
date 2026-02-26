"""Logging functions for the market-maker engine.

Each function inserts rows into the MM analytics tables.
Follows the same pattern as db_logger.py.
"""

import time
import db


def log_quote(ticker, side, price, size, action):
    """Log a quote placement, cancel, or update."""
    conn = db.get_connection()
    try:
        conn.execute(
            "INSERT INTO mm_quotes (timestamp, ticker, side, price, size, action) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (time.time(), ticker, side, price, size, action),
        )
        conn.commit()
    finally:
        conn.close()


def log_fill(ticker, side, price, count, inventory_after, realized_pnl_cum):
    """Log a detected fill."""
    conn = db.get_connection()
    try:
        conn.execute(
            "INSERT INTO mm_fills (timestamp, ticker, side, price, count, "
            "inventory_after, realized_pnl_cumulative) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (time.time(), ticker, side, price, count, inventory_after, realized_pnl_cum),
        )
        conn.commit()
    finally:
        conn.close()


def log_snapshot(cycle, strikes_dict, total_rpnl):
    """Log a cycle snapshot — one row per strike.

    strikes_dict: {ticker: StrikeState} with strike, bid_price, ask_price,
                  inventory, realized_pnl fields.
    """
    now = time.time()
    conn = db.get_connection()
    try:
        rows = []
        for ticker, st in strikes_dict.items():
            rows.append((
                now, cycle, ticker, st.strike,
                st.bid_price, st.ask_price,
                st.inventory, st.realized_pnl, total_rpnl,
            ))
        if rows:
            conn.executemany(
                "INSERT INTO mm_snapshots "
                "(timestamp, cycle, ticker, strike, bid_price, ask_price, "
                "inventory, strike_realized_pnl, total_realized_pnl) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            conn.commit()
    finally:
        conn.close()
