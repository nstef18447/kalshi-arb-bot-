"""SQLite schema and connection helpers for Kalshi arb bot analytics."""

import os
import sqlite3

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "arb_bot.db")

# Tables only — indexes created after migrations
SCHEMA_TABLES = """
CREATE TABLE IF NOT EXISTS scans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    series_ticker TEXT NOT NULL DEFAULT '',
    expiry_window TEXT NOT NULL,
    num_strikes INTEGER NOT NULL,
    scan_duration_ms REAL
);

CREATE TABLE IF NOT EXISTS ladder_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    series_ticker TEXT NOT NULL DEFAULT '',
    expiry_window TEXT NOT NULL,
    strike REAL NOT NULL,
    yes_ask INTEGER NOT NULL,
    yes_bid INTEGER NOT NULL,
    no_ask INTEGER NOT NULL,
    no_bid INTEGER NOT NULL,
    yes_depth INTEGER NOT NULL,
    no_depth INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS opportunities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    series_ticker TEXT NOT NULL DEFAULT '',
    expiry_window TEXT NOT NULL,
    opp_type TEXT NOT NULL,
    sub_type TEXT NOT NULL,
    strike_low REAL NOT NULL,
    strike_high REAL NOT NULL,
    yes_ask_low INTEGER NOT NULL,
    no_ask_high INTEGER NOT NULL,
    combined_cost INTEGER NOT NULL,
    estimated_profit REAL NOT NULL,
    estimated_profit_maker REAL,
    btc_price_at_detection REAL,
    time_to_expiry_seconds REAL,
    depth_thin_side INTEGER
);

CREATE TABLE IF NOT EXISTS arb_stability (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    series_ticker TEXT NOT NULL DEFAULT '',
    expiry_window TEXT NOT NULL,
    strike_low REAL NOT NULL,
    strike_high REAL NOT NULL,
    combined_cost INTEGER NOT NULL,
    depth_thin_side INTEGER,
    first_seen REAL NOT NULL,
    scan_count INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'open',
    close_reason TEXT
);

CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    expiry_window TEXT NOT NULL,
    opp_type TEXT NOT NULL,
    strike_low REAL NOT NULL,
    strike_high REAL NOT NULL,
    leg1_side TEXT NOT NULL,
    leg1_price INTEGER NOT NULL,
    leg1_fill_status TEXT NOT NULL,
    leg2_side TEXT,
    leg2_price INTEGER,
    leg2_fill_status TEXT,
    orphaned INTEGER NOT NULL DEFAULT 0,
    exit_price INTEGER,
    realized_pnl REAL,
    fees REAL
);

CREATE TABLE IF NOT EXISTS binary_arb_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    ticker TEXT NOT NULL,
    yes_price INTEGER NOT NULL,
    no_price INTEGER NOT NULL,
    combined_cost INTEGER NOT NULL,
    size INTEGER NOT NULL,
    yes_order_id TEXT,
    no_order_id TEXT,
    yes_filled INTEGER NOT NULL DEFAULT 0,
    no_filled INTEGER NOT NULL DEFAULT 0,
    hedge_action TEXT,
    realized_pnl REAL,
    fees REAL
);

CREATE TABLE IF NOT EXISTS mm_quotes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    ticker TEXT NOT NULL,
    side TEXT NOT NULL,
    price INTEGER NOT NULL,
    size INTEGER NOT NULL,
    action TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS mm_fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    ticker TEXT NOT NULL,
    side TEXT NOT NULL,
    price INTEGER NOT NULL,
    count INTEGER NOT NULL,
    inventory_after INTEGER NOT NULL,
    realized_pnl_cumulative REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS mm_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    cycle INTEGER NOT NULL,
    ticker TEXT NOT NULL,
    strike REAL NOT NULL,
    bid_price INTEGER,
    ask_price INTEGER,
    inventory INTEGER NOT NULL DEFAULT 0,
    strike_realized_pnl REAL NOT NULL DEFAULT 0,
    total_realized_pnl REAL NOT NULL DEFAULT 0
);
"""

# Indexes — created after migrations so columns exist
SCHEMA_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_scans_ts ON scans(timestamp);
CREATE INDEX IF NOT EXISTS idx_scans_expiry ON scans(expiry_window);
CREATE INDEX IF NOT EXISTS idx_scans_series ON scans(series_ticker);
CREATE INDEX IF NOT EXISTS idx_ladder_ts ON ladder_snapshots(timestamp);
CREATE INDEX IF NOT EXISTS idx_ladder_expiry ON ladder_snapshots(expiry_window);
CREATE INDEX IF NOT EXISTS idx_ladder_expiry_ts ON ladder_snapshots(expiry_window, timestamp);
CREATE INDEX IF NOT EXISTS idx_ladder_series ON ladder_snapshots(series_ticker);
CREATE INDEX IF NOT EXISTS idx_opps_ts ON opportunities(timestamp);
CREATE INDEX IF NOT EXISTS idx_opps_type ON opportunities(opp_type, sub_type);
CREATE INDEX IF NOT EXISTS idx_opps_expiry ON opportunities(expiry_window);
CREATE INDEX IF NOT EXISTS idx_opps_series ON opportunities(series_ticker);
CREATE INDEX IF NOT EXISTS idx_trades_ts ON trades(timestamp);
CREATE INDEX IF NOT EXISTS idx_stability_ts ON arb_stability(timestamp);
CREATE INDEX IF NOT EXISTS idx_stability_status ON arb_stability(status);
CREATE INDEX IF NOT EXISTS idx_stability_pair ON arb_stability(expiry_window, strike_low, strike_high);
CREATE INDEX IF NOT EXISTS idx_bat_ts ON binary_arb_trades(timestamp);
CREATE INDEX IF NOT EXISTS idx_bat_ticker ON binary_arb_trades(ticker);
CREATE INDEX IF NOT EXISTS idx_mm_quotes_ts ON mm_quotes(timestamp);
CREATE INDEX IF NOT EXISTS idx_mm_quotes_ticker ON mm_quotes(ticker);
CREATE INDEX IF NOT EXISTS idx_mm_fills_ts ON mm_fills(timestamp);
CREATE INDEX IF NOT EXISTS idx_mm_fills_ticker ON mm_fills(ticker);
CREATE INDEX IF NOT EXISTS idx_mm_snapshots_ts ON mm_snapshots(timestamp);
CREATE INDEX IF NOT EXISTS idx_mm_snapshots_cycle ON mm_snapshots(cycle);
"""


def get_connection(readonly=False):
    """Get a SQLite connection. WAL mode for concurrent read/write."""
    if readonly:
        uri = f"file:{DB_PATH}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=5)
    else:
        conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Create all tables, run migrations, then create indexes."""
    conn = get_connection()
    try:
        # 1. Create tables (IF NOT EXISTS — safe for existing DBs)
        conn.executescript(SCHEMA_TABLES)

        # 2. Migrations for existing DBs — add missing columns
        opp_cols = {row[1] for row in conn.execute("PRAGMA table_info(opportunities)").fetchall()}
        if "estimated_profit_maker" not in opp_cols:
            conn.execute("ALTER TABLE opportunities ADD COLUMN estimated_profit_maker REAL")
        if "series_ticker" not in opp_cols:
            conn.execute("ALTER TABLE opportunities ADD COLUMN series_ticker TEXT NOT NULL DEFAULT 'KXBTC'")

        scan_cols = {row[1] for row in conn.execute("PRAGMA table_info(scans)").fetchall()}
        if "series_ticker" not in scan_cols:
            conn.execute("ALTER TABLE scans ADD COLUMN series_ticker TEXT NOT NULL DEFAULT 'KXBTC'")

        snap_cols = {row[1] for row in conn.execute("PRAGMA table_info(ladder_snapshots)").fetchall()}
        if "series_ticker" not in snap_cols:
            conn.execute("ALTER TABLE ladder_snapshots ADD COLUMN series_ticker TEXT NOT NULL DEFAULT 'KXBTC'")

        conn.commit()

        # 3. Create indexes (columns now guaranteed to exist)
        conn.executescript(SCHEMA_INDEXES)
        conn.commit()
    finally:
        conn.close()
