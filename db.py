"""SQLite schema and connection helpers for Kalshi arb bot analytics."""

import os
import sqlite3

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "arb_bot.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS scans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    expiry_window TEXT NOT NULL,
    num_strikes INTEGER NOT NULL,
    scan_duration_ms REAL
);

CREATE TABLE IF NOT EXISTS ladder_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
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

CREATE INDEX IF NOT EXISTS idx_scans_ts ON scans(timestamp);
CREATE INDEX IF NOT EXISTS idx_scans_expiry ON scans(expiry_window);
CREATE INDEX IF NOT EXISTS idx_ladder_ts ON ladder_snapshots(timestamp);
CREATE INDEX IF NOT EXISTS idx_ladder_expiry ON ladder_snapshots(expiry_window);
CREATE INDEX IF NOT EXISTS idx_ladder_expiry_ts ON ladder_snapshots(expiry_window, timestamp);
CREATE INDEX IF NOT EXISTS idx_opps_ts ON opportunities(timestamp);
CREATE INDEX IF NOT EXISTS idx_opps_type ON opportunities(opp_type, sub_type);
CREATE INDEX IF NOT EXISTS idx_opps_expiry ON opportunities(expiry_window);
CREATE INDEX IF NOT EXISTS idx_trades_ts ON trades(timestamp);
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
    """Create all tables and indexes if they don't exist."""
    conn = get_connection()
    try:
        conn.executescript(SCHEMA)
        # Migrate: add estimated_profit_maker if missing (existing DBs)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(opportunities)").fetchall()}
        if "estimated_profit_maker" not in cols:
            conn.execute("ALTER TABLE opportunities ADD COLUMN estimated_profit_maker REAL")
        conn.commit()
    finally:
        conn.close()
