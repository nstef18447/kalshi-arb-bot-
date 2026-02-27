"""Investigation script: KXBTC15M market structure + long-duration orderbook scan.

Answers:
1. How many events/markets exist for KXBTC15M at any given time?
2. What do the tickers, strikes, and event structures look like?
3. Over 30+ minutes, does the combined yes+no ask ever dip to 96 or below?
"""

import os
import sys
import time
import json
import sqlite3
from datetime import datetime, timezone

from dotenv import load_dotenv
load_dotenv()

import kalshi_api
import db

# --- Config ---
SCAN_DURATION_MINUTES = int(os.getenv("SCAN_MINUTES", "30"))
POLL_INTERVAL = 1
SERIES = "KXBTC15M"


def init_scan_log_table():
    """Create a temp table for raw scan logging."""
    conn = db.get_connection()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS binary_arb_scans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp REAL NOT NULL,
            scan_num INTEGER NOT NULL,
            ticker TEXT NOT NULL,
            event_ticker TEXT,
            subtitle TEXT,
            close_time TEXT,
            yes_ask INTEGER,
            no_ask INTEGER,
            combined INTEGER,
            yes_depth INTEGER,
            no_depth INTEGER,
            yes_bid INTEGER,
            no_bid INTEGER,
            book_empty INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_bas_ts ON binary_arb_scans(timestamp);
        CREATE INDEX IF NOT EXISTS idx_bas_ticker ON binary_arb_scans(ticker);
    """)
    conn.commit()
    conn.close()


def log_scan_row(scan_num, ticker, event_ticker, subtitle, close_time,
                 yes_ask, no_ask, combined, yes_depth, no_depth,
                 yes_bid, no_bid, book_empty):
    conn = db.get_connection()
    conn.execute(
        "INSERT INTO binary_arb_scans "
        "(timestamp, scan_num, ticker, event_ticker, subtitle, close_time, "
        "yes_ask, no_ask, combined, yes_depth, no_depth, yes_bid, no_bid, book_empty) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (time.time(), scan_num, ticker, event_ticker, subtitle, close_time,
         yes_ask, no_ask, combined, yes_depth, no_depth, yes_bid, no_bid, book_empty),
    )
    conn.commit()
    conn.close()


def dump_market_structure():
    """Print the full event/market hierarchy for KXBTC15M."""
    print(f"\n{'='*70}")
    print(f"  KXBTC15M MARKET STRUCTURE DUMP")
    print(f"  Time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC")
    print(f"{'='*70}\n")

    # --- Events ---
    print("--- EVENTS (series_ticker=KXBTC15M) ---")
    events = kalshi_api.get_events(SERIES, status="open")
    print(f"  Found {len(events)} open events\n")

    for i, ev in enumerate(events):
        et = ev.get("event_ticker", "")
        title = ev.get("title", "")
        subtitle = ev.get("sub_title", ev.get("subtitle", ""))
        category = ev.get("category", "")
        status = ev.get("status", "")
        # Print key fields
        print(f"  Event #{i+1}: {et}")
        print(f"    title:    {title}")
        print(f"    subtitle: {subtitle}")
        print(f"    category: {category}")
        print(f"    status:   {status}")

        # Fetch markets for this event
        markets = kalshi_api.get_markets_for_event(et, status="open")
        print(f"    markets:  {len(markets)} open")
        for j, m in enumerate(markets):
            ticker = m.get("ticker", "")
            m_subtitle = m.get("subtitle", m.get("sub_title", ""))
            m_title = m.get("title", m.get("short_title", ""))
            close_time = m.get("close_time", "")
            open_time = m.get("open_time", "")
            floor = m.get("floor_strike", m.get("custom_strike", ""))
            cap = m.get("cap_strike", "")
            yes_sub = m.get("yes_sub_title", "")
            no_sub = m.get("no_sub_title", "")
            print(f"      Market #{j+1}: {ticker}")
            print(f"        title:      {m_title}")
            print(f"        subtitle:   {m_subtitle}")
            print(f"        yes_sub:    {yes_sub}")
            print(f"        no_sub:     {no_sub}")
            print(f"        open:       {open_time}")
            print(f"        close:      {close_time}")
            print(f"        floor:      {floor}")
            print(f"        cap:        {cap}")

            # Also dump first market's full JSON for field discovery
            if i == 0 and j == 0:
                print(f"\n  --- FULL JSON of first market (field discovery) ---")
                for k, v in sorted(m.items()):
                    print(f"        {k}: {v}")
                print(f"  --- END FULL JSON ---\n")

        print()

    # --- Also try direct markets query ---
    print("--- DIRECT MARKETS QUERY (series_ticker=KXBTC15M, status=open) ---")
    direct = kalshi_api._paginate(
        "/trade-api/v2/markets",
        {"series_ticker": SERIES, "status": "open", "limit": 200},
        "markets",
    )
    print(f"  Found {len(direct)} markets via direct query\n")
    for m in direct:
        ticker = m.get("ticker", "")
        event = m.get("event_ticker", "")
        subtitle = m.get("subtitle", "")
        close = m.get("close_time", "")
        print(f"    {ticker:40s} event={event:30s} close={close}  {subtitle}")

    # --- Also check: are there other statuses? ---
    print("\n--- CHECKING OTHER STATUSES ---")
    for status in ["closed", "settled"]:
        try:
            other = kalshi_api.get_events(SERIES, status=status)
            print(f"  {status}: {len(other)} events")
            if other and len(other) <= 5:
                for ev in other:
                    print(f"    {ev.get('event_ticker', '')}: {ev.get('title', '')}")
        except Exception as e:
            print(f"  {status}: error - {e}")

    print(f"\n{'='*70}\n")


def run_long_scan():
    """Run orderbook scan for SCAN_DURATION_MINUTES, logging everything."""
    end_time = time.monotonic() + SCAN_DURATION_MINUTES * 60
    scan_num = 0
    all_combined = []
    opportunities = []

    print(f"Starting {SCAN_DURATION_MINUTES}-minute scan at "
          f"{datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC")
    print(f"Threshold check: combined <= 98 will be flagged\n")

    while time.monotonic() < end_time:
        scan_num += 1
        scan_start = time.monotonic()

        try:
            markets = kalshi_api.get_markets(SERIES, status="open")
        except Exception as e:
            print(f"  [scan #{scan_num}] API error fetching markets: {e}")
            time.sleep(POLL_INTERVAL)
            continue

        market_count = len(markets)

        for market in markets:
            ticker = market.get("ticker", "")
            event_ticker = market.get("event_ticker", "")
            subtitle = market.get("subtitle", "")
            close_time = market.get("close_time", "")

            if not ticker:
                continue

            try:
                book = kalshi_api.get_orderbook(ticker, depth=1)
            except Exception:
                log_scan_row(scan_num, ticker, event_ticker, subtitle, close_time,
                             None, None, None, None, None, None, None, book_empty=1)
                continue

            yes_bids = book.get("yes", [])
            no_bids = book.get("no", [])

            if not yes_bids or not no_bids:
                log_scan_row(scan_num, ticker, event_ticker, subtitle, close_time,
                             None, None, None, None, None, None, None, book_empty=1)
                continue

            yes_ask = 100 - no_bids[0][0]
            no_ask = 100 - yes_bids[0][0]
            yes_depth = no_bids[0][1]
            no_depth = yes_bids[0][1]
            yes_bid = yes_bids[0][0]
            no_bid = no_bids[0][0]
            combined = yes_ask + no_ask

            log_scan_row(scan_num, ticker, event_ticker, subtitle, close_time,
                         yes_ask, no_ask, combined, yes_depth, no_depth,
                         yes_bid, no_bid, book_empty=0)

            all_combined.append(combined)

            # Flag anything at or below 98
            flag = ""
            if combined <= 96:
                flag = " *** ARB OPPORTUNITY (<=96) ***"
                opportunities.append((scan_num, ticker, yes_ask, no_ask, combined,
                                      yes_depth, no_depth))
            elif combined <= 98:
                flag = " ** NEAR MISS (<=98) **"
                opportunities.append((scan_num, ticker, yes_ask, no_ask, combined,
                                      yes_depth, no_depth))

            # Print every scan line
            now_str = datetime.now(timezone.utc).strftime("%H:%M:%S")
            print(
                f"  [{now_str}] scan#{scan_num:4d} {ticker:35s} "
                f"yes={yes_ask:3d} no={no_ask:3d} comb={combined:3d} "
                f"yd={yes_depth:5d} nd={no_depth:5d}{flag}"
            )

        # Print scan summary every 60 scans (~5 min)
        if scan_num % 60 == 0 and all_combined:
            elapsed = SCAN_DURATION_MINUTES - (end_time - time.monotonic()) / 60
            print(f"\n  === {elapsed:.0f} min elapsed | {scan_num} scans | "
                  f"{len(all_combined)} observations | "
                  f"min={min(all_combined)} max={max(all_combined)} "
                  f"avg={sum(all_combined)/len(all_combined):.1f} | "
                  f"{len(opportunities)} flagged ===\n")

        scan_duration = time.monotonic() - scan_start
        sleep_time = max(0, POLL_INTERVAL - scan_duration)
        time.sleep(sleep_time)

    # --- Final Summary ---
    print(f"\n{'='*70}")
    print(f"  SCAN COMPLETE — {scan_num} scans over {SCAN_DURATION_MINUTES} minutes")
    print(f"{'='*70}")

    if all_combined:
        print(f"  Observations:     {len(all_combined)}")
        print(f"  Min combined:     {min(all_combined)}")
        print(f"  Max combined:     {max(all_combined)}")
        print(f"  Avg combined:     {sum(all_combined)/len(all_combined):.2f}")
        print(f"  Median combined:  {sorted(all_combined)[len(all_combined)//2]}")

        # Distribution
        buckets = {}
        for c in all_combined:
            buckets[c] = buckets.get(c, 0) + 1
        print(f"\n  Combined cost distribution:")
        for cost in sorted(buckets.keys()):
            bar = "#" * min(50, buckets[cost])
            pct = buckets[cost] / len(all_combined) * 100
            print(f"    {cost:3d}: {buckets[cost]:5d} ({pct:5.1f}%) {bar}")

        # Flagged opportunities
        sub98 = [c for c in all_combined if c <= 98]
        sub96 = [c for c in all_combined if c <= 96]
        print(f"\n  Combined <= 98:   {len(sub98)} observations")
        print(f"  Combined <= 96:   {len(sub96)} observations")

        if opportunities:
            print(f"\n  Flagged opportunities:")
            for scan, ticker, ya, na, comb, yd, nd in opportunities:
                print(f"    scan#{scan} {ticker} yes={ya} no={na} "
                      f"combined={comb} yd={yd} nd={nd}")
    else:
        print(f"  No orderbook data collected (all books were empty)")

    # DB row count
    conn = db.get_connection(readonly=True)
    count = conn.execute("SELECT COUNT(*) FROM binary_arb_scans").fetchone()[0]
    conn.close()
    print(f"\n  DB rows in binary_arb_scans: {count}")
    print(f"{'='*70}\n")


if __name__ == "__main__":
    print(f"Current time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC")
    print(f"KALSHI_ENV: {os.getenv('KALSHI_ENV', 'demo')}")

    init_scan_log_table()

    # Phase 1: dump market structure
    dump_market_structure()

    # Phase 2: long-duration scan
    run_long_scan()
