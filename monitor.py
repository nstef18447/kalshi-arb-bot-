"""Market Maker monitoring script — reads from DB + Kalshi API.

Usage:
    python3 monitor.py              # default: last 24 hours
    python3 monitor.py --hours 48   # custom window
    python3 monitor.py --compact    # one-line summary
"""

import argparse
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone

# Add project dir to path so we can import project modules
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db

# Try to import API functions for live data (optional — works without)
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
    from kalshi_api import get_balance, get_open_orders, get_positions
    HAS_API = True
except Exception:
    HAS_API = False


def get_conn():
    return db.get_connection(readonly=True)


def utcnow():
    return datetime.now(timezone.utc)


def ts_cutoff(hours):
    return time.time() - hours * 3600


def day_start_ts():
    """Midnight UTC today as unix timestamp."""
    now = utcnow()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.timestamp()


# ------------------------------------------------------------------
# Data queries
# ------------------------------------------------------------------

def query_fills(conn, since_ts):
    """Get all fills since timestamp."""
    rows = conn.execute(
        "SELECT timestamp, ticker, side, price, count, inventory_after, "
        "realized_pnl_cumulative FROM mm_fills WHERE timestamp >= ? ORDER BY timestamp",
        (since_ts,),
    ).fetchall()
    return [dict(r) for r in rows]


def query_snapshots_latest(conn):
    """Get the most recent snapshot per ticker (current state)."""
    rows = conn.execute(
        "SELECT ticker, strike, bid_price, ask_price, inventory, "
        "strike_realized_pnl, total_realized_pnl, timestamp "
        "FROM mm_snapshots WHERE cycle = (SELECT MAX(cycle) FROM mm_snapshots) "
        "ORDER BY strike",
    ).fetchall()
    return [dict(r) for r in rows]


def query_quotes_stats(conn, since_ts):
    """Count place vs cancel actions for requote stats."""
    rows = conn.execute(
        "SELECT action, COUNT(*) as cnt FROM mm_quotes "
        "WHERE timestamp >= ? GROUP BY action",
        (since_ts,),
    ).fetchall()
    return {r["action"]: r["cnt"] for r in rows}


def query_snapshots_hourly(conn, since_ts):
    """Get hourly aggregates from snapshots."""
    rows = conn.execute(
        """SELECT
            CAST((timestamp - ?) / 3600 AS INTEGER) as hour_offset,
            MAX(total_realized_pnl) as max_rpnl,
            MIN(total_realized_pnl) as min_rpnl,
            MAX(ABS(inventory)) as max_inv,
            AVG(bid_price) as avg_bid,
            AVG(ask_price) as avg_ask,
            COUNT(DISTINCT cycle) as cycles
        FROM mm_snapshots
        WHERE timestamp >= ?
        GROUP BY hour_offset
        ORDER BY hour_offset""",
        (since_ts, since_ts),
    ).fetchall()
    return [dict(r) for r in rows]


def query_fills_hourly(conn, since_ts):
    """Get hourly fill counts."""
    rows = conn.execute(
        """SELECT
            CAST((timestamp - ?) / 3600 AS INTEGER) as hour_offset,
            COUNT(*) as fill_count,
            SUM(count) as total_contracts
        FROM mm_fills
        WHERE timestamp >= ?
        GROUP BY hour_offset
        ORDER BY hour_offset""",
        (since_ts, since_ts),
    ).fetchall()
    return {r["hour_offset"]: dict(r) for r in rows}


def query_all_time_stats(conn):
    """Get all-time cumulative stats."""
    row = conn.execute(
        "SELECT MIN(timestamp) as first_ts, MAX(timestamp) as last_ts, "
        "COUNT(*) as total_fills, SUM(count) as total_contracts "
        "FROM mm_fills"
    ).fetchone()
    return dict(row) if row else {}


def query_daily_pnl(conn):
    """Get realized P&L by day (from last rpnl_cumulative per day)."""
    rows = conn.execute(
        """SELECT
            DATE(timestamp, 'unixepoch') as day,
            MAX(realized_pnl_cumulative) as end_rpnl,
            MIN(realized_pnl_cumulative) as start_rpnl_approx
        FROM mm_fills
        GROUP BY day
        ORDER BY day"""
    ).fetchall()
    return [dict(r) for r in rows]


# ------------------------------------------------------------------
# Compute derived metrics
# ------------------------------------------------------------------

def compute_fill_stats(fills):
    """Compute fill statistics from a list of fill dicts."""
    if not fills:
        return {
            "buy_fills": 0, "sell_fills": 0,
            "buy_contracts": 0, "sell_contracts": 0,
            "avg_buy_price": 0, "avg_sell_price": 0,
            "round_trips": 0, "avg_spread": 0,
            "realized_pnl": 0, "fees_est": 0,
        }

    buy_fills = [f for f in fills if f["side"] == "yes"]
    sell_fills = [f for f in fills if f["side"] == "no"]

    buy_contracts = sum(f["count"] for f in buy_fills)
    sell_contracts = sum(f["count"] for f in sell_fills)

    avg_buy = (sum(f["price"] * f["count"] for f in buy_fills) / buy_contracts
               if buy_contracts > 0 else 0)
    avg_sell = (sum(f["price"] * f["count"] for f in sell_fills) / sell_contracts
                if sell_contracts > 0 else 0)

    round_trips = min(buy_contracts, sell_contracts)
    avg_spread = (100 - avg_buy - avg_sell) if round_trips > 0 else 0

    # Realized P&L from last cumulative value
    realized_pnl = fills[-1]["realized_pnl_cumulative"] if fills else 0
    # If there were fills before our window, subtract the starting value
    if len(fills) > 0:
        # Use the change in cumulative P&L over the window
        realized_pnl = fills[-1]["realized_pnl_cumulative"] - (
            fills[0]["realized_pnl_cumulative"] if len(fills) > 1 else 0
        )
        # Actually the first fill in window already includes its own contribution
        # The most accurate is: last_cumulative - value_just_before_first_fill
        # For simplicity, if we only have fills in this window, use last value
        realized_pnl = fills[-1]["realized_pnl_cumulative"]

    # Estimate fees: maker_mult * P * (1-P) per contract
    maker_mult = 0.0175  # KXBTCD default
    fees = 0.0
    for f in fills:
        p = f["price"]
        fees += maker_mult * p * (100 - p) / 100 * f["count"]

    return {
        "buy_fills": len(buy_fills),
        "sell_fills": len(sell_fills),
        "buy_contracts": buy_contracts,
        "sell_contracts": sell_contracts,
        "avg_buy_price": avg_buy,
        "avg_sell_price": avg_sell,
        "round_trips": round_trips,
        "avg_spread": avg_spread,
        "realized_pnl": realized_pnl,
        "fees_est": fees,
    }


def compute_unrealized(fills, snapshots):
    """Estimate unrealized P&L from current inventory and unmatched fills."""
    total_upnl = 0.0
    for snap in snapshots:
        inv = snap["inventory"]
        if inv == 0:
            continue
        # Assume unmatched inventory can close at 50c (mid)
        # This is a rough estimate
        if inv > 0:
            # Long yes — unmatched yes fills
            yes_fills = [f for f in fills if f["side"] == "yes" and f["ticker"] == snap["ticker"]]
            if yes_fills:
                avg_price = sum(f["price"] * f["count"] for f in yes_fills) / max(1, sum(f["count"] for f in yes_fills))
                total_upnl += inv * (50 - avg_price)
        else:
            no_fills = [f for f in fills if f["side"] == "no" and f["ticker"] == snap["ticker"]]
            if no_fills:
                avg_price = sum(f["price"] * f["count"] for f in no_fills) / max(1, sum(f["count"] for f in no_fills))
                total_upnl += abs(inv) * (50 - avg_price)
    return total_upnl


# ------------------------------------------------------------------
# Output formatting
# ------------------------------------------------------------------

def print_compact(fills, snapshots, hours):
    """One-line compact output."""
    stats = compute_fill_stats(fills)
    total_inv = sum(s["inventory"] for s in snapshots)
    upnl = compute_unrealized(fills, snapshots)
    net = stats["realized_pnl"] + upnl - stats["fees_est"]

    # Quote stats
    quote_stats = {}
    try:
        conn = get_conn()
        quote_stats = query_quotes_stats(conn, ts_cutoff(hours))
        conn.close()
    except Exception:
        pass

    places = quote_stats.get("place", 0)
    cancels = quote_stats.get("cancel", 0)
    total_quotes = places + cancels
    # Each requote = 1 cancel + 1 place. Skips = cycles that didn't requote.
    # Approximate: requotes = cancels, total_decisions = places (each place is a decision)
    skip_pct = 0
    if places > 0:
        # places includes initial placements + requotes
        # cancels happen only on requotes, so requotes ≈ cancels
        # skips ≈ places - cancels (places that weren't preceded by a cancel)
        skip_pct = max(0, (1 - cancels / places) * 100) if places > 0 else 0

    avg_spread = stats["avg_spread"]
    total_fills = stats["buy_fills"] + stats["sell_fills"]

    print(f"[MM] net=${net/100:+.2f} | fills={total_fills} | "
          f"inv={total_inv:+d} | spread={avg_spread:.1f}c | "
          f"rpnl=${stats['realized_pnl']/100:+.2f}")


def print_full(fills, hours):
    """Full detailed output."""
    conn = get_conn()
    now = utcnow()
    since_ts = ts_cutoff(hours)
    today_ts = day_start_ts()

    snapshots = query_snapshots_latest(conn)
    quote_stats = query_quotes_stats(conn, since_ts)
    hourly_snaps = query_snapshots_hourly(conn, since_ts)
    hourly_fills = query_fills_hourly(conn, since_ts)
    all_time = query_all_time_stats(conn)
    daily_pnl = query_daily_pnl(conn)
    conn.close()

    stats = compute_fill_stats(fills)
    upnl = compute_unrealized(fills, snapshots)
    net = stats["realized_pnl"] + upnl - stats["fees_est"]

    print(f"\n{'='*64}")
    print(f"MARKET MAKER STATUS — {now.strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f"{'='*64}")

    # --- ACCOUNT ---
    print(f"\nACCOUNT")
    if HAS_API:
        try:
            balance = get_balance()
            print(f"  Balance: ${balance/100:.2f}")
        except Exception as e:
            print(f"  Balance: (API error: {e})")

        try:
            orders = get_open_orders()
            if orders:
                print(f"  Open orders: {len(orders)}")
                for o in orders[:5]:
                    side = o.get("side", "?")
                    price = o.get("yes_price") or o.get("no_price", 0)
                    ticker = o.get("ticker", "?")
                    remaining = o.get("remaining_count", 0)
                    print(f"    {side}@{price}c x{remaining} on {ticker}")
            else:
                print(f"  Open orders: 0")
        except Exception as e:
            print(f"  Open orders: (API error: {e})")

        try:
            positions = get_positions()
            if positions:
                for p in positions:
                    ticker = p.get("ticker", "?")
                    qty = p.get("position", 0)
                    avg_cost = p.get("market_exposure", 0)
                    print(f"  Position: {qty:+d} on {ticker}")
            else:
                print(f"  Positions: 0")
        except Exception as e:
            print(f"  Positions: (API error: {e})")
    else:
        print(f"  (API not available — run from bot directory with .env)")

    # Current inventory from snapshots
    if snapshots:
        print(f"\n  Current strikes:")
        for s in snapshots:
            ticker_short = s["ticker"].split("-")[-1] if "-" in s["ticker"] else s["ticker"]
            print(f"    {ticker_short} ({s['strike']:.0f}): "
                  f"bid={s['bid_price'] or 0} ask={s['ask_price'] or 0} "
                  f"inv={s['inventory']:+d} rpnl={s['strike_realized_pnl']:+.0f}c")

    # --- P&L ---
    window_label = f"LAST {hours}H" if hours != 24 else "TODAY"
    print(f"\n{window_label}'S P&L")
    print(f"  Realized P&L:    ${stats['realized_pnl']/100:+.2f} "
          f"({stats['round_trips']} round trips)")
    print(f"  Unrealized P&L:  ${upnl/100:+.2f} "
          f"({sum(abs(s['inventory']) for s in snapshots)} contracts open)")
    print(f"  Fees (est):      ${stats['fees_est']/100:.2f}")
    print(f"  Net P&L:         ${net/100:+.2f}")

    # --- FILLS ---
    print(f"\nFILLS ({window_label})")
    print(f"  Buy fills:  {stats['buy_fills']} events, "
          f"{stats['buy_contracts']} contracts "
          f"(avg {stats['avg_buy_price']:.1f}c)")
    print(f"  Sell fills: {stats['sell_fills']} events, "
          f"{stats['sell_contracts']} contracts "
          f"(avg {stats['avg_sell_price']:.1f}c)")
    if stats["round_trips"] > 0:
        print(f"  Avg spread captured: {stats['avg_spread']:.1f}c")
    else:
        print(f"  Avg spread captured: — (no round trips)")

    # --- PERFORMANCE ---
    print(f"\nPERFORMANCE")
    if stats["round_trips"] > 0:
        gross_per_rt = stats["avg_spread"]
        fees_per_rt = stats["fees_est"] / stats["round_trips"]
        net_per_rt = gross_per_rt - fees_per_rt
        print(f"  Gross per round trip:  {gross_per_rt:.1f}c")
        print(f"  Net per round trip:    {net_per_rt:.1f}c")
    else:
        print(f"  (No round trips in window)")

    max_inv = 0
    if snapshots:
        try:
            conn2 = get_conn()
            row = conn2.execute(
                "SELECT MAX(ABS(inventory)) as max_inv FROM mm_snapshots WHERE timestamp >= ?",
                (since_ts,)
            ).fetchone()
            max_inv = row["max_inv"] if row and row["max_inv"] else 0
            conn2.close()
        except Exception:
            pass
    print(f"  Max inventory held:    {max_inv} contracts")

    # Quote stats
    places = quote_stats.get("place", 0)
    cancels = quote_stats.get("cancel", 0)
    if places > 0:
        skip_rate = max(0, (1 - cancels / places)) * 100
        print(f"  Quotes placed:         {places}")
        print(f"  Quotes cancelled:      {cancels}")
        print(f"  Requote skip rate:     {skip_rate:.0f}%")

    # --- HOURLY BREAKDOWN ---
    if hourly_snaps:
        print(f"\nHOURLY BREAKDOWN ({window_label})")
        start_time = datetime.fromtimestamp(since_ts, tz=timezone.utc)
        prev_rpnl = 0
        for h in hourly_snaps:
            offset = h["hour_offset"]
            hour_time = start_time.replace(minute=0, second=0, microsecond=0)
            hour_ts = hour_time.timestamp() + offset * 3600
            hour_dt = datetime.fromtimestamp(hour_ts, tz=timezone.utc)
            hour_str = hour_dt.strftime("%H:%M")

            fill_info = hourly_fills.get(offset, {"fill_count": 0, "total_contracts": 0})
            rpnl_change = h["max_rpnl"] - prev_rpnl
            prev_rpnl = h["max_rpnl"]

            avg_bid = h["avg_bid"] or 0
            avg_ask = h["avg_ask"] or 0
            avg_spread = (avg_ask - avg_bid) if avg_bid > 0 and avg_ask > 0 else 0

            print(f"  {hour_str}  fills={fill_info['fill_count']:>2}  "
                  f"rpnl={rpnl_change:>+6.0f}c  "
                  f"spread={avg_spread:>4.0f}c  "
                  f"max_inv={h['max_inv']}")

    # --- CUMULATIVE ---
    print(f"\nCUMULATIVE (all time)")
    if all_time and all_time.get("first_ts"):
        days_live = (time.time() - all_time["first_ts"]) / 86400
        total_fills = all_time.get("total_fills", 0)

        # Get final cumulative P&L
        conn3 = get_conn()
        row = conn3.execute(
            "SELECT realized_pnl_cumulative FROM mm_fills ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()
        total_rpnl = row["realized_pnl_cumulative"] if row else 0

        # Estimate total fees from all fills
        all_fills = conn3.execute(
            "SELECT price, count FROM mm_fills"
        ).fetchall()
        total_fees = sum(0.0175 * r["price"] * (100 - r["price"]) / 100 * r["count"]
                         for r in all_fills)
        conn3.close()

        total_net = total_rpnl - total_fees
        avg_daily = total_net / days_live if days_live > 0 else 0

        print(f"  Days live:        {days_live:.1f}")
        print(f"  Total fills:      {total_fills}")
        print(f"  Total realized:   ${total_rpnl/100:+.2f}")
        print(f"  Total fees (est): ${total_fees/100:.2f}")
        print(f"  Total net:        ${total_net/100:+.2f}")
        print(f"  Avg daily net:    ${avg_daily/100:.2f}")

        if daily_pnl:
            best = max(daily_pnl, key=lambda d: d["end_rpnl"] - d.get("start_rpnl_approx", 0))
            worst = min(daily_pnl, key=lambda d: d["end_rpnl"] - d.get("start_rpnl_approx", 0))
            # Approximate daily P&L from cumulative changes
            if len(daily_pnl) > 1:
                daily_changes = []
                for i in range(1, len(daily_pnl)):
                    change = daily_pnl[i]["end_rpnl"] - daily_pnl[i-1]["end_rpnl"]
                    daily_changes.append((daily_pnl[i]["day"], change))
                if daily_changes:
                    best_day = max(daily_changes, key=lambda x: x[1])
                    worst_day = min(daily_changes, key=lambda x: x[1])
                    print(f"  Best day:         {best_day[0]} ${best_day[1]/100:+.2f}")
                    print(f"  Worst day:        {worst_day[0]} ${worst_day[1]/100:+.2f}")
    else:
        print(f"  (No fill data yet)")

    print(f"\n{'='*64}\n")


# ------------------------------------------------------------------
# Main
# ------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Market Maker Monitor")
    parser.add_argument("--hours", type=float, default=24, help="Lookback window (default: 24)")
    parser.add_argument("--compact", action="store_true", help="One-line summary")
    args = parser.parse_args()

    since_ts = ts_cutoff(args.hours)

    conn = get_conn()
    fills = query_fills(conn, since_ts)
    snapshots = query_snapshots_latest(conn)
    conn.close()

    if args.compact:
        print_compact(fills, snapshots, args.hours)
    else:
        print_full(fills, args.hours)


if __name__ == "__main__":
    main()
