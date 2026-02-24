"""Diagnostic script: find ALL KXBTC15M markets on Kalshi.

Tries multiple API approaches to identify why the bot only sees 1 market.
Run: python test_markets.py

Requires .env with KALSHI_API_KEY, KALSHI_PRIVATE_KEY_PATH, KALSHI_ENV.
"""

import os
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()

from auth import authenticated_request

SERIES = "KXBTC15M"
DIVIDER = "=" * 70


def safe_request(method, path, params=None):
    """Make an API request, returning {} on error."""
    try:
        return authenticated_request(method, path, params=params)
    except Exception as e:
        print(f"  ERROR: {e}")
        return {}


def fetch_all_pages(path, params, items_key):
    """Paginate through all results for a given endpoint."""
    all_items = []
    cursor = None
    page = 0
    while True:
        p = dict(params)
        if cursor:
            p["cursor"] = cursor
        data = safe_request("GET", path, params=p)
        items = data.get(items_key, [])
        all_items.extend(items)
        page += 1
        cursor = data.get("cursor")
        if not cursor or not items:
            break
    return all_items


def test_1_current_approach():
    """Exactly what the bot does today."""
    print(f"\n{DIVIDER}")
    print(f"TEST 1: Current bot approach")
    print(f"  GET /trade-api/v2/markets?series_ticker={SERIES}&status=open&limit=200")
    print(DIVIDER)

    markets = fetch_all_pages(
        "/trade-api/v2/markets",
        {"series_ticker": SERIES, "status": "open", "limit": 200},
        "markets",
    )
    print(f"  Found: {len(markets)} markets")
    for m in markets[:10]:
        print(f"    {m.get('ticker', '?'):40s}  strike={m.get('floor_strike', '?'):>10s}  "
              f"expiry={m.get('latest_expiration_time', '?')}")
    if len(markets) > 10:
        print(f"    ... and {len(markets) - 10} more")
    return markets


def test_2_no_status_filter():
    """Remove status filter to see if markets exist but aren't 'open'."""
    print(f"\n{DIVIDER}")
    print(f"TEST 2: No status filter (all statuses)")
    print(f"  GET /trade-api/v2/markets?series_ticker={SERIES}&limit=200")
    print(DIVIDER)

    markets = fetch_all_pages(
        "/trade-api/v2/markets",
        {"series_ticker": SERIES, "limit": 200},
        "markets",
    )
    by_status = {}
    for m in markets:
        s = m.get("status", "unknown")
        by_status.setdefault(s, []).append(m)

    print(f"  Found: {len(markets)} total markets")
    for status, group in sorted(by_status.items()):
        print(f"    {status}: {len(group)}")
    return markets


def test_3_events_then_markets():
    """Use the events endpoint first, then fetch markets per event."""
    print(f"\n{DIVIDER}")
    print(f"TEST 3: Events API -> Markets per event")
    print(f"  Step 1: GET /trade-api/v2/events?series_ticker={SERIES}&status=open")
    print(DIVIDER)

    events = fetch_all_pages(
        "/trade-api/v2/events",
        {"series_ticker": SERIES, "status": "open", "limit": 200},
        "events",
    )
    print(f"  Found: {len(events)} open events")

    all_markets = []
    for ev in events:
        et = ev.get("event_ticker", "?")
        title = ev.get("title", "")
        print(f"\n  Event: {et}")
        print(f"  Title: {title}")

        markets = fetch_all_pages(
            "/trade-api/v2/markets",
            {"event_ticker": et, "status": "open", "limit": 200},
            "markets",
        )
        print(f"  Markets: {len(markets)}")
        for m in markets:
            strike = m.get("floor_strike", "?")
            expiry = m.get("latest_expiration_time", "?")
            ticker = m.get("ticker", "?")
            print(f"    {ticker:40s}  strike={str(strike):>10s}  expiry={expiry}")
        all_markets.extend(markets)

    print(f"\n  Total markets across all events: {len(all_markets)}")
    return events, all_markets


def test_4_broader_search():
    """Search without series_ticker, using ticker prefix instead."""
    print(f"\n{DIVIDER}")
    print(f"TEST 4: Broader search — all open markets with ticker containing KXBTC")
    print(f"  GET /trade-api/v2/markets?status=open&limit=200")
    print(DIVIDER)

    markets = fetch_all_pages(
        "/trade-api/v2/markets",
        {"status": "open", "limit": 200},
        "markets",
    )
    kxbtc = [m for m in markets if "KXBTC" in m.get("ticker", "").upper()]
    print(f"  Total open markets: {len(markets)}")
    print(f"  KXBTC markets: {len(kxbtc)}")

    # Group by series
    by_series = {}
    for m in kxbtc:
        st = m.get("series_ticker", "unknown")
        by_series.setdefault(st, []).append(m)

    for series, group in sorted(by_series.items()):
        print(f"\n  Series: {series} ({len(group)} markets)")
        for m in group[:5]:
            print(f"    {m.get('ticker', '?'):40s}  strike={str(m.get('floor_strike', '?')):>10s}")
        if len(group) > 5:
            print(f"    ... and {len(group) - 5} more")

    return kxbtc


def test_5_series_info():
    """Fetch the series object itself to see its metadata."""
    print(f"\n{DIVIDER}")
    print(f"TEST 5: Series metadata")
    print(f"  GET /trade-api/v2/series/{SERIES}")
    print(DIVIDER)

    data = safe_request("GET", f"/trade-api/v2/series/{SERIES}")
    series = data.get("series", data)
    if series:
        for k, v in series.items():
            print(f"    {k}: {v}")
    else:
        print("  Series not found! The series ticker might be wrong.")
        print("  Trying without '15M' suffix...")
        data = safe_request("GET", "/trade-api/v2/series/KXBTC")
        series = data.get("series", data)
        if series:
            for k, v in series.items():
                print(f"    {k}: {v}")


def test_6_orderbook_check(markets):
    """For the markets we found, check if orderbooks have liquidity."""
    if not markets:
        print("\n  Skipping orderbook check — no markets to test.")
        return

    print(f"\n{DIVIDER}")
    print(f"TEST 6: Orderbook liquidity check (first 5 markets)")
    print(DIVIDER)

    for m in markets[:5]:
        ticker = m.get("ticker", "?")
        try:
            data = authenticated_request(
                "GET", f"/trade-api/v2/markets/{ticker}/orderbook",
                params={"depth": 3},
            )
            book = data.get("orderbook", {})
            yes_levels = book.get("yes", [])
            no_levels = book.get("no", [])
            print(f"  {ticker}")
            print(f"    Yes bids: {yes_levels}")
            print(f"    No  bids: {no_levels}")
            has_both = bool(yes_levels) and bool(no_levels)
            print(f"    Has both sides: {'YES' if has_both else 'NO'}")
        except Exception as e:
            print(f"  {ticker}: ERROR fetching orderbook — {e}")


def main():
    env = os.getenv("KALSHI_ENV", "demo").upper()
    print(f"Kalshi Market Diagnostic — {env} environment")
    print(f"Searching for series: {SERIES}")
    print(f"Time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC")

    # Run all tests
    markets_1 = test_1_current_approach()
    markets_2 = test_2_no_status_filter()
    events_3, markets_3 = test_3_events_then_markets()
    kxbtc_4 = test_4_broader_search()
    test_5_series_info()
    test_6_orderbook_check(markets_3 or markets_1 or kxbtc_4)

    # Summary
    print(f"\n{DIVIDER}")
    print("DIAGNOSIS SUMMARY")
    print(DIVIDER)
    print(f"  Test 1 (current approach, series_ticker filter): {len(markets_1)} markets")
    print(f"  Test 2 (no status filter):                       {len(markets_2)} markets")
    print(f"  Test 3 (events -> markets):                      {len(markets_3)} markets across {len(events_3)} events")
    print(f"  Test 4 (broad search, ticker contains KXBTC):    {len(kxbtc_4)} markets")

    if len(markets_3) > len(markets_1):
        print(f"\n  >>> LIKELY FIX: Use events API first, then fetch markets per event.")
        print(f"      The series_ticker param on /markets returns {len(markets_1)},")
        print(f"      but events->markets returns {len(markets_3)}.")
    elif len(kxbtc_4) > len(markets_1):
        print(f"\n  >>> LIKELY FIX: The series_ticker '{SERIES}' doesn't match.")
        print(f"      Broader search found {len(kxbtc_4)} KXBTC markets.")
        series_tickers = {m.get("series_ticker") for m in kxbtc_4}
        print(f"      Actual series tickers: {series_tickers}")
    elif len(markets_1) <= 1:
        print(f"\n  >>> Only {len(markets_1)} market found. Possible causes:")
        print(f"      - Markets haven't opened yet (check Kalshi hours)")
        print(f"      - Demo environment has limited data")
        print(f"      - Series ticker '{SERIES}' is wrong")
    else:
        print(f"\n  >>> Markets look fine ({len(markets_1)} found). Issue may be elsewhere.")


if __name__ == "__main__":
    main()
