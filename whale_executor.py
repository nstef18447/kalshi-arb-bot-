"""Whale signal executor — follows high-conviction Polymarket whale trades on Kalshi.

Polls the Polymarket tracker DB for new TRADEABLE whale alerts matching
strict filters, then places BUY orders on Kalshi in the whale's direction.

High-conviction filters:
- signal_quality = 'TRADEABLE'
- wallet_tier = 'ELITE'
- kalshi_ticker starts with KXNBAGAME (NBA moneyline)
- price_bucket = 'midrange'
- price_gap > 0 (Kalshi is cheaper than Poly)
"""

import logging
import os
import sqlite3
import time
from datetime import datetime, timezone

import config
import db
import kalshi_api

logger = logging.getLogger("whale-executor")

# --- Configuration ---
POLY_DB_PATH = os.getenv(
    "POLY_DB_PATH",
    "/var/lib/docker/volumes/polymarket-data/_data/polymarket.db",
)
BET_SIZE_DOLLARS = float(os.getenv("WHALE_BET_SIZE", "2.00"))
POLL_INTERVAL = int(os.getenv("WHALE_POLL_INTERVAL", "30"))
ORDER_EXPIRY_SECONDS = int(os.getenv("WHALE_ORDER_EXPIRY", "300"))  # 5 min
MAX_ALERT_AGE_SECONDS = int(os.getenv("WHALE_MAX_ALERT_AGE", "300"))  # only trade alerts < 5 min old

# Ticker prefixes allowed for execution
ALLOWED_PREFIXES = os.getenv("WHALE_ALLOWED_PREFIXES", "KXNBAGAME").split(",")
# Wallet tiers allowed
ALLOWED_TIERS = os.getenv("WHALE_ALLOWED_TIERS", "ELITE").split(",")
# Price buckets allowed
ALLOWED_BUCKETS = os.getenv("WHALE_ALLOWED_BUCKETS", "midrange").split(",")
# Minimum gap threshold — negative means we allow Kalshi to be slightly more expensive
# Default: -0.05 (allow up to 5c more expensive on Kalshi)
MIN_GAP = float(os.getenv("WHALE_MIN_GAP", "-0.05"))


def _init_whale_tables():
    """Create whale_orders table if it doesn't exist."""
    conn = db.get_connection()
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS whale_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                whale_alert_id INTEGER NOT NULL,
                kalshi_ticker TEXT NOT NULL,
                side TEXT NOT NULL,
                price_cents INTEGER NOT NULL,
                count INTEGER NOT NULL,
                order_id TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                filled_count INTEGER DEFAULT 0,
                filled_price INTEGER,
                cancelled_at REAL,
                expires_at REAL NOT NULL,
                resolved_at REAL,
                resolved_result TEXT,
                pnl_cents INTEGER,
                poly_market_title TEXT,
                poly_side TEXT,
                poly_outcome TEXT,
                poly_price REAL,
                wallet TEXT,
                wallet_tier TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_whale_orders_alert ON whale_orders(whale_alert_id);
            CREATE INDEX IF NOT EXISTS idx_whale_orders_status ON whale_orders(status);
            CREATE INDEX IF NOT EXISTS idx_whale_orders_ticker ON whale_orders(kalshi_ticker);
        """)
        conn.commit()
    finally:
        conn.close()


def _get_poly_connection():
    """Open read-only connection to the Polymarket tracker DB."""
    uri = f"file:{POLY_DB_PATH}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _get_executed_alert_ids() -> set[int]:
    """Get IDs of whale alerts we've already placed orders for."""
    conn = db.get_connection(readonly=True)
    try:
        rows = conn.execute("SELECT whale_alert_id FROM whale_orders").fetchall()
        return {r[0] for r in rows}
    finally:
        conn.close()


def _determine_kalshi_side(poly_side: str, poly_outcome: str) -> str:
    """Determine which Kalshi side to buy based on whale's Poly trade.

    BUY YES → buy 'yes' on Kalshi
    BUY NO  → buy 'no' on Kalshi
    SELL YES → buy 'no' on Kalshi
    SELL NO  → buy 'yes' on Kalshi
    """
    if poly_side.upper() == "BUY":
        return "yes" if poly_outcome.lower() in ("yes", "y") else "no"
    else:
        return "no" if poly_outcome.lower() in ("yes", "y") else "yes"


def _fetch_new_alerts() -> list[dict]:
    """Fetch new high-conviction TRADEABLE alerts from Polymarket DB."""
    try:
        conn = _get_poly_connection()
    except Exception as e:
        logger.warning("Cannot connect to Polymarket DB: %s", e)
        return []

    try:
        executed = _get_executed_alert_ids()

        # Build prefix filter
        prefix_conditions = " OR ".join(
            f"kalshi_ticker LIKE '{p}%'" for p in ALLOWED_PREFIXES
        )
        tier_list = ",".join(f"'{t}'" for t in ALLOWED_TIERS)
        bucket_list = ",".join(f"'{b}'" for b in ALLOWED_BUCKETS)

        query = f"""
            SELECT id, kalshi_ticker, side, outcome, poly_price, price_gap,
                   market_title, wallet, wallet_tier, price_bucket, timestamp,
                   signal_quality
            FROM whale_alerts
            WHERE signal_quality = 'TRADEABLE'
              AND wallet_tier IN ({tier_list})
              AND price_bucket IN ({bucket_list})
              AND ({prefix_conditions})
              AND kalshi_ticker IS NOT NULL
              AND timestamp > datetime('now', '-{MAX_ALERT_AGE_SECONDS} seconds')
        """

        query += f" AND price_gap >= {MIN_GAP}"

        query += " ORDER BY timestamp DESC"

        rows = conn.execute(query).fetchall()

        alerts = []
        for r in rows:
            if r["id"] in executed:
                continue
            alerts.append(dict(r))

        return alerts
    finally:
        conn.close()


def _place_order(alert: dict) -> dict | None:
    """Place a BUY order on Kalshi following the whale's direction."""
    ticker = alert["kalshi_ticker"]
    kalshi_side = _determine_kalshi_side(alert["side"], alert["outcome"])

    # Fetch current orderbook to get best price
    # Kalshi orderbook: yes = YES bids, no = NO bids
    # YES ask = 100 - best NO bid, NO ask = 100 - best YES bid
    try:
        book = kalshi_api.get_orderbook(ticker, depth=3)
    except Exception as e:
        logger.warning("Failed to fetch orderbook for %s: %s", ticker, e)
        return None

    yes_bids = book.get("yes", [])
    no_bids = book.get("no", [])

    if kalshi_side == "yes":
        # To buy YES, we need NO bids to exist (YES ask = 100 - NO bid)
        if not no_bids:
            logger.info("No NO bids for %s — can't buy YES, skipping", ticker)
            return None
        best_ask_cents = 100 - no_bids[0][0]
    else:
        # To buy NO, we need YES bids to exist (NO ask = 100 - YES bid)
        if not yes_bids:
            logger.info("No YES bids for %s — can't buy NO, skipping", ticker)
            return None
        best_ask_cents = 100 - yes_bids[0][0]

    if best_ask_cents <= 0 or best_ask_cents >= 100:
        logger.info("Invalid ask price %d for %s — skipping", best_ask_cents, ticker)
        return None

    # Calculate contract count: floor($BET_SIZE / price_in_dollars)
    price_dollars = best_ask_cents / 100.0
    count = int(BET_SIZE_DOLLARS / price_dollars)
    if count < 1:
        count = 1  # minimum 1 contract

    now = time.time()
    expires_at = now + ORDER_EXPIRY_SECONDS

    logger.info(
        "PLACING ORDER: %s %s %dc @ %dc (%d contracts, $%.2f risk) — whale %s %s on '%s'",
        "BUY", kalshi_side.upper(), count, best_ask_cents, count,
        count * price_dollars, alert["side"], alert["outcome"],
        (alert["market_title"] or "")[:50],
    )

    # Record in DB first (status=pending)
    conn = db.get_connection()
    try:
        conn.execute(
            """INSERT INTO whale_orders
               (timestamp, whale_alert_id, kalshi_ticker, side, price_cents, count,
                status, expires_at, poly_market_title, poly_side, poly_outcome,
                poly_price, wallet, wallet_tier)
               VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?)""",
            (now, alert["id"], ticker, kalshi_side, best_ask_cents, count,
             expires_at, alert["market_title"], alert["side"], alert["outcome"],
             alert["poly_price"], alert["wallet"], alert["wallet_tier"]),
        )
        order_db_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.commit()
    finally:
        conn.close()

    if config.READ_ONLY:
        logger.info("READ_ONLY mode — order logged but not placed (id=%d)", order_db_id)
        conn = db.get_connection()
        try:
            conn.execute(
                "UPDATE whale_orders SET status = 'paper' WHERE id = ?",
                (order_db_id,),
            )
            conn.commit()
        finally:
            conn.close()
        return {"db_id": order_db_id, "status": "paper"}

    # Place the actual order
    try:
        result = kalshi_api.create_order(
            ticker=ticker,
            side=kalshi_side,
            price_cents=best_ask_cents,
            count=count,
        )
        order_id = result.get("order_id", "")
        status = result.get("status", "open")

        conn = db.get_connection()
        try:
            conn.execute(
                "UPDATE whale_orders SET order_id = ?, status = ? WHERE id = ?",
                (order_id, status, order_db_id),
            )
            conn.commit()
        finally:
            conn.close()

        logger.info("Order placed: id=%s status=%s", order_id, status)
        return {"db_id": order_db_id, "order_id": order_id, "status": status}

    except Exception as e:
        logger.error("ORDER FAILED for %s: %s", ticker, e)
        conn = db.get_connection()
        try:
            conn.execute(
                "UPDATE whale_orders SET status = 'error' WHERE id = ?",
                (order_db_id,),
            )
            conn.commit()
        finally:
            conn.close()
        return None


def _check_open_orders():
    """Check status of open orders, cancel expired ones, update fills."""
    conn = db.get_connection()
    try:
        open_orders = conn.execute(
            "SELECT id, order_id, kalshi_ticker, expires_at FROM whale_orders WHERE status = 'open'"
        ).fetchall()
    finally:
        conn.close()

    now = time.time()
    for row in open_orders:
        db_id, order_id, ticker, expires_at = row["id"], row["order_id"], row["kalshi_ticker"], row["expires_at"]

        if not order_id:
            continue

        # Check fill status
        try:
            order = kalshi_api.get_order(order_id)
            status = order.get("status", "")
            filled = order.get("filled_count", 0) or 0
            filled_price = order.get("average_fill_price", None)

            if status == "filled" or (status == "closed" and filled > 0):
                conn = db.get_connection()
                try:
                    conn.execute(
                        """UPDATE whale_orders
                           SET status = 'filled', filled_count = ?, filled_price = ?
                           WHERE id = ?""",
                        (filled, filled_price, db_id),
                    )
                    conn.commit()
                finally:
                    conn.close()
                logger.info("ORDER FILLED: %s %d contracts @ %sc", ticker, filled, filled_price)
                continue

            if status in ("canceled", "cancelled"):
                conn = db.get_connection()
                try:
                    conn.execute(
                        "UPDATE whale_orders SET status = 'cancelled', cancelled_at = ? WHERE id = ?",
                        (now, db_id),
                    )
                    conn.commit()
                finally:
                    conn.close()
                continue

        except Exception as e:
            logger.warning("Failed to check order %s: %s", order_id, e)

        # Cancel if expired
        if now > expires_at:
            logger.info("Cancelling expired order %s for %s", order_id, ticker)
            try:
                kalshi_api.cancel_order(order_id)
            except Exception:
                pass
            conn = db.get_connection()
            try:
                conn.execute(
                    "UPDATE whale_orders SET status = 'expired', cancelled_at = ? WHERE id = ?",
                    (now, db_id),
                )
                conn.commit()
            finally:
                conn.close()


def _check_resolutions():
    """Check if filled whale orders have resolved, compute P&L."""
    conn = db.get_connection()
    try:
        filled_orders = conn.execute(
            """SELECT id, kalshi_ticker, side, filled_price, filled_count
               FROM whale_orders WHERE status = 'filled'"""
        ).fetchall()
    finally:
        conn.close()

    for row in filled_orders:
        db_id = row["id"]
        ticker = row["kalshi_ticker"]
        side = row["side"]
        fill_price = row["filled_price"] or row["id"]  # fallback shouldn't happen
        fill_count = row["filled_count"] or 0

        try:
            market = kalshi_api.get_market(ticker)
        except Exception:
            continue

        if not market.get("result"):
            continue  # not settled yet

        result = market["result"].lower()  # "yes" or "no"
        won = (result == side)

        if won:
            pnl = (100 - fill_price) * fill_count  # payout minus cost
        else:
            pnl = -fill_price * fill_count  # lost the cost

        conn = db.get_connection()
        try:
            conn.execute(
                """UPDATE whale_orders
                   SET status = 'resolved', resolved_at = ?, resolved_result = ?, pnl_cents = ?
                   WHERE id = ?""",
                (time.time(), result, pnl, db_id),
            )
            conn.commit()
        finally:
            conn.close()

        logger.info(
            "RESOLVED: %s result=%s %s | PnL=%+dc ($%+.2f) | %d contracts @ %dc",
            ticker, result, "WIN" if won else "LOSS", pnl, pnl / 100, fill_count, fill_price,
        )


class WhaleExecutor:
    """Main loop: poll for whale alerts, place orders, manage lifecycle."""

    def __init__(self):
        self.running = False
        _init_whale_tables()

    def start(self):
        self.running = True
        logger.info("Whale executor started (bet=$%.2f, poll=%ds)", BET_SIZE_DOLLARS, POLL_INTERVAL)
        logger.info("Filters: prefixes=%s, tiers=%s, buckets=%s, min_gap=%.2f",
                     ALLOWED_PREFIXES, ALLOWED_TIERS, ALLOWED_BUCKETS, MIN_GAP)

        cycle = 0
        while self.running:
            try:
                cycle += 1

                # 1. Check for new qualifying alerts
                alerts = _fetch_new_alerts()
                if alerts:
                    logger.info("Found %d new qualifying whale alert(s)", len(alerts))
                    for alert in alerts:
                        _place_order(alert)

                # 2. Manage open orders (every cycle)
                if not config.READ_ONLY:
                    _check_open_orders()

                # 3. Check resolutions (every 10th cycle = ~5 min)
                if cycle % 10 == 0:
                    _check_resolutions()

                time.sleep(POLL_INTERVAL)

            except KeyboardInterrupt:
                break
            except Exception:
                logger.exception("Whale executor cycle failed")
                time.sleep(POLL_INTERVAL)

    def stop(self):
        self.running = False
        logger.info("Whale executor stopped")
