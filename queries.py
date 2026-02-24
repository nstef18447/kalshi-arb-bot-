"""All SQL queries for the dashboard, returning pandas DataFrames."""

import pandas as pd
import db


def _read_sql(sql, params=None):
    """Execute a read-only query and return a DataFrame."""
    conn = db.get_connection(readonly=True)
    try:
        df = pd.read_sql_query(sql, conn, params=params or [])
        return df
    finally:
        conn.close()


# ── Overview page ──────────────────────────────────────────────────


def get_opp_counts():
    """Total opportunities all-time and last 24h, broken down by type."""
    return _read_sql("""
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN timestamp > unixepoch('now') - 86400 THEN 1 ELSE 0 END) AS last_24h,
            SUM(CASE WHEN opp_type = 'A' THEN 1 ELSE 0 END) AS type_a,
            SUM(CASE WHEN opp_type = 'B' THEN 1 ELSE 0 END) AS type_b,
            SUM(CASE WHEN opp_type = 'C' AND sub_type = 'hard' THEN 1 ELSE 0 END) AS c_hard,
            SUM(CASE WHEN opp_type = 'C' AND sub_type = 'soft' THEN 1 ELSE 0 END) AS c_soft,
            SUM(CASE WHEN opp_type = 'A' AND timestamp > unixepoch('now') - 86400 THEN 1 ELSE 0 END) AS type_a_24h,
            SUM(CASE WHEN opp_type = 'B' AND timestamp > unixepoch('now') - 86400 THEN 1 ELSE 0 END) AS type_b_24h,
            SUM(CASE WHEN opp_type = 'C' AND sub_type = 'hard' AND timestamp > unixepoch('now') - 86400 THEN 1 ELSE 0 END) AS c_hard_24h,
            SUM(CASE WHEN opp_type = 'C' AND sub_type = 'soft' AND timestamp > unixepoch('now') - 86400 THEN 1 ELSE 0 END) AS c_soft_24h
        FROM opportunities
    """)


def get_avg_hard_arb_spread():
    """Average spread in cents for hard arbs (100 - combined_cost)."""
    return _read_sql("""
        SELECT AVG(100 - combined_cost) AS avg_spread
        FROM opportunities
        WHERE opp_type = 'C' AND sub_type = 'hard'
    """)


def get_opps_per_hour(date_start=None, date_end=None, opp_types=None, min_spread=0):
    """Opportunities detected per hour, broken down by opp_type + sub_type."""
    where = ["1=1"]
    params = []

    if date_start:
        where.append("timestamp >= ?")
        params.append(date_start)
    if date_end:
        where.append("timestamp <= ?")
        params.append(date_end)
    if opp_types:
        placeholders = ",".join("?" for _ in opp_types)
        where.append(f"(opp_type || '_' || sub_type) IN ({placeholders})")
        params.extend(opp_types)
    if min_spread > 0:
        where.append("(100 - combined_cost) >= ?")
        params.append(min_spread)

    sql = f"""
        SELECT
            CAST(timestamp / 3600 AS INTEGER) * 3600 AS hour_ts,
            opp_type || '_' || sub_type AS label,
            COUNT(*) AS count
        FROM opportunities
        WHERE {' AND '.join(where)}
        GROUP BY hour_ts, label
        ORDER BY hour_ts
    """
    return _read_sql(sql, params)


def get_hard_arb_spread_distribution(date_start=None, date_end=None):
    """Distribution of spread sizes for hard arbs (for histogram)."""
    where = ["opp_type = 'C' AND sub_type = 'hard'"]
    params = []
    if date_start:
        where.append("timestamp >= ?")
        params.append(date_start)
    if date_end:
        where.append("timestamp <= ?")
        params.append(date_end)

    sql = f"""
        SELECT (100 - combined_cost) AS spread_cents
        FROM opportunities
        WHERE {' AND '.join(where)}
    """
    return _read_sql(sql, params)


def get_spread_vs_expiry(date_start=None, date_end=None, opp_types=None):
    """Spread size vs time-to-expiry for scatter plot."""
    where = ["time_to_expiry_seconds IS NOT NULL"]
    params = []
    if date_start:
        where.append("timestamp >= ?")
        params.append(date_start)
    if date_end:
        where.append("timestamp <= ?")
        params.append(date_end)
    if opp_types:
        placeholders = ",".join("?" for _ in opp_types)
        where.append(f"(opp_type || '_' || sub_type) IN ({placeholders})")
        params.extend(opp_types)

    sql = f"""
        SELECT
            (100 - combined_cost) AS spread_cents,
            time_to_expiry_seconds,
            opp_type || '_' || sub_type AS label
        FROM opportunities
        WHERE {' AND '.join(where)}
    """
    return _read_sql(sql, params)


def get_opp_persistence():
    """Median persistence time — seconds between first and last detection of same opp.

    "Same opp" = same expiry_window + opp_type + strike_low + strike_high.
    """
    return _read_sql("""
        SELECT
            expiry_window, opp_type, sub_type, strike_low, strike_high,
            MIN(timestamp) AS first_seen,
            MAX(timestamp) AS last_seen,
            MAX(timestamp) - MIN(timestamp) AS persistence_seconds,
            COUNT(*) AS detection_count
        FROM opportunities
        GROUP BY expiry_window, opp_type, sub_type, strike_low, strike_high
        HAVING COUNT(*) > 1
    """)


# ── Ladder Explorer page ──────────────────────────────────────────


def get_expiry_windows():
    """All distinct expiry windows in the DB."""
    return _read_sql("""
        SELECT DISTINCT expiry_window
        FROM ladder_snapshots
        ORDER BY expiry_window DESC
    """)


def get_snapshot_timestamps(expiry_window):
    """All distinct timestamps for a given expiry window."""
    return _read_sql("""
        SELECT DISTINCT timestamp
        FROM ladder_snapshots
        WHERE expiry_window = ?
        ORDER BY timestamp DESC
    """, [expiry_window])


def get_ladder_at_timestamp(expiry_window, timestamp):
    """Full strike ladder for a specific window and timestamp."""
    return _read_sql("""
        SELECT strike, yes_ask, yes_bid, no_ask, no_bid, yes_depth, no_depth,
               (yes_ask + no_ask) AS combined
        FROM ladder_snapshots
        WHERE expiry_window = ? AND timestamp = ?
        ORDER BY strike ASC
    """, [expiry_window, timestamp])


def get_ladder_heatmap_data(expiry_window, limit=60):
    """Last N snapshots for heatmap: timestamp × strike → yes_ask."""
    return _read_sql("""
        SELECT timestamp, strike, yes_ask
        FROM ladder_snapshots
        WHERE expiry_window = ?
          AND timestamp IN (
              SELECT DISTINCT timestamp
              FROM ladder_snapshots
              WHERE expiry_window = ?
              ORDER BY timestamp DESC
              LIMIT ?
          )
        ORDER BY timestamp, strike
    """, [expiry_window, expiry_window, limit])


def get_opps_for_window(expiry_window):
    """All opportunities detected for a specific expiry window."""
    return _read_sql("""
        SELECT timestamp, opp_type, sub_type, strike_low, strike_high,
               combined_cost, estimated_profit
        FROM opportunities
        WHERE expiry_window = ?
        ORDER BY timestamp
    """, [expiry_window])


# ── Cross-Strike Matrix page ─────────────────────────────────────


def get_matrix_data(expiry_window, timestamp):
    """All strikes at a specific timestamp for building the NxN matrix."""
    return _read_sql("""
        SELECT strike, yes_ask, no_ask
        FROM ladder_snapshots
        WHERE expiry_window = ? AND timestamp = ?
        ORDER BY strike ASC
    """, [expiry_window, timestamp])


# ── Trade Log page ────────────────────────────────────────────────


def get_all_trades():
    """All trades, newest first."""
    return _read_sql("""
        SELECT id, timestamp, expiry_window, opp_type,
               strike_low, strike_high,
               leg1_side, leg1_price, leg1_fill_status,
               leg2_side, leg2_price, leg2_fill_status,
               orphaned, exit_price, realized_pnl, fees
        FROM trades
        ORDER BY timestamp DESC
    """)


def get_trade_summary():
    """Aggregate trade stats."""
    return _read_sql("""
        SELECT
            COUNT(*) AS total_trades,
            SUM(CASE WHEN leg2_fill_status = 'filled' AND orphaned = 0 THEN 1 ELSE 0 END) AS wins,
            SUM(CASE WHEN orphaned = 1 THEN 1 ELSE 0 END) AS orphans,
            COALESCE(SUM(realized_pnl), 0) AS total_pnl,
            COALESCE(SUM(fees), 0) AS total_fees
        FROM trades
    """)


def get_cumulative_pnl():
    """Cumulative P&L over time for equity curve."""
    return _read_sql("""
        SELECT timestamp, realized_pnl,
               SUM(COALESCE(realized_pnl, 0)) OVER (ORDER BY timestamp) AS cumulative_pnl
        FROM trades
        ORDER BY timestamp
    """)


def get_rolling_orphan_rate(window=20):
    """Rolling orphan rate over the last N trades."""
    return _read_sql("""
        SELECT timestamp, orphaned,
               AVG(orphaned) OVER (ORDER BY timestamp ROWS BETWEEN ? PRECEDING AND CURRENT ROW)
                   AS rolling_orphan_rate
        FROM trades
        ORDER BY timestamp
    """, [window - 1])


# ── Competition Signals page ──────────────────────────────────────


def get_persistence_over_time():
    """Rolling 1-hour median persistence time, computed per hour bucket."""
    return _read_sql("""
        WITH opp_groups AS (
            SELECT
                expiry_window, opp_type, sub_type, strike_low, strike_high,
                MIN(timestamp) AS first_seen,
                MAX(timestamp) AS last_seen,
                MAX(timestamp) - MIN(timestamp) AS persistence_seconds
            FROM opportunities
            GROUP BY expiry_window, opp_type, sub_type, strike_low, strike_high
            HAVING COUNT(*) > 1
        )
        SELECT
            CAST(first_seen / 3600 AS INTEGER) * 3600 AS hour_ts,
            persistence_seconds
        FROM opp_groups
        ORDER BY hour_ts
    """)


def get_avg_spread_over_time():
    """Average hard arb spread per hour."""
    return _read_sql("""
        SELECT
            CAST(timestamp / 3600 AS INTEGER) * 3600 AS hour_ts,
            AVG(100 - combined_cost) AS avg_spread
        FROM opportunities
        WHERE opp_type = 'C' AND sub_type = 'hard'
        GROUP BY hour_ts
        ORDER BY hour_ts
    """)


def get_flash_opps():
    """Opportunities that appeared once and disappeared (detected in one scan only)."""
    return _read_sql("""
        SELECT
            CAST(timestamp / 3600 AS INTEGER) * 3600 AS hour_ts,
            COUNT(*) AS flash_count
        FROM (
            SELECT expiry_window, opp_type, sub_type, strike_low, strike_high,
                   COUNT(*) AS cnt, MIN(timestamp) AS timestamp
            FROM opportunities
            GROUP BY expiry_window, opp_type, sub_type, strike_low, strike_high
            HAVING cnt = 1
        )
        GROUP BY hour_ts
        ORDER BY hour_ts
    """)


def get_time_of_day_breakdown():
    """Opportunity count and avg spread by hour of day (UTC)."""
    return _read_sql("""
        SELECT
            CAST((timestamp % 86400) / 3600 AS INTEGER) AS hour_utc,
            COUNT(*) AS opp_count,
            AVG(CASE WHEN opp_type = 'C' AND sub_type = 'hard' THEN 100 - combined_cost END) AS avg_spread
        FROM opportunities
        GROUP BY hour_utc
        ORDER BY hour_utc
    """)


def get_db_info():
    """Row counts for each table."""
    conn = db.get_connection(readonly=True)
    try:
        info = {}
        for table in ("scans", "ladder_snapshots", "opportunities", "trades"):
            row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
            info[table] = row[0]
        return info
    finally:
        conn.close()
