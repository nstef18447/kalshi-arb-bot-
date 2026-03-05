"""Probe Kalshi API for trade history, volume, and market metadata fields."""
import sys, os, json, time
sys.path.insert(0, "/opt/kalshi-arb-bot")
from auth import authenticated_request
from kalshi_api import get_events, get_markets_for_event

BASE = "/trade-api/v2"

def probe(method, path, params=None, label=""):
    """Try an endpoint, return (status_code, data) or (status_code, error)."""
    print(f"\n{'='*60}")
    print(f"PROBE: {label}")
    print(f"  {method} {path}")
    if params:
        print(f"  params: {params}")
    try:
        import requests
        from auth import _sign_request, API_KEY, BASE_URL
        ts = int(time.time() * 1000)
        sig = _sign_request(method, path, ts)
        headers = {
            "KALSHI-ACCESS-KEY": API_KEY,
            "KALSHI-ACCESS-SIGNATURE": sig,
            "KALSHI-ACCESS-TIMESTAMP": str(ts),
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        url = BASE_URL.replace("/trade-api/v2", "") + path
        resp = requests.request(method, url, headers=headers, params=params, timeout=10)
        print(f"  STATUS: {resp.status_code}")
        if resp.status_code == 200:
            data = resp.json()
            # Pretty print first 2000 chars
            txt = json.dumps(data, indent=2)
            print(txt[:3000])
            if len(txt) > 3000:
                print(f"  ... ({len(txt)} total chars)")
            return resp.status_code, data
        else:
            print(f"  BODY: {resp.text[:500]}")
            return resp.status_code, resp.text
    except Exception as e:
        print(f"  ERROR: {e}")
        return 0, str(e)


def find_active_tickers():
    """Find one active KXBTCD ticker and one active KXBTC ticker."""
    results = {}
    for series in ["KXBTCD", "KXBTC"]:
        print(f"\nFinding active {series} market...")
        events = get_events(series)
        if not events:
            print(f"  No events for {series}")
            continue
        # Pick first event with open markets
        for ev in events[:3]:
            markets = get_markets_for_event(ev["event_ticker"])
            open_markets = [m for m in markets if m.get("status") == "open"]
            if open_markets:
                # Pick one near ATM if possible
                mid_idx = len(open_markets) // 2
                m = open_markets[mid_idx]
                results[series] = {
                    "ticker": m["ticker"],
                    "event": ev["event_ticker"],
                    "strike": m.get("floor_strike"),
                    "close_time": m.get("close_time"),
                }
                print(f"  Selected: {m['ticker']} (strike {m.get('floor_strike')}, closes {m.get('close_time')})")
                break
    return results


def inspect_market_fields(ticker):
    """Fetch a single market and dump ALL fields."""
    print(f"\n{'='*60}")
    print(f"FULL MARKET OBJECT: {ticker}")
    try:
        data = authenticated_request("GET", f"{BASE}/markets/{ticker}")
        market = data.get("market", data)
        print(json.dumps(market, indent=2))

        # Highlight volume/trade related fields
        interesting = {}
        for k, v in market.items():
            kl = k.lower()
            if any(term in kl for term in ["volume", "trade", "interest", "count", "last", "price", "notional", "liquidity"]):
                interesting[k] = v
        if interesting:
            print(f"\n  ** VOLUME/TRADE FIELDS FOUND **")
            for k, v in interesting.items():
                print(f"    {k}: {v}")
        else:
            print(f"\n  ** No volume/trade fields found in market object **")
        return market
    except Exception as e:
        print(f"  ERROR: {e}")
        return {}


def main():
    print("=" * 60)
    print("KALSHI API VOLUME/TRADE PROBE")
    print("=" * 60)

    # Step 1: Find active tickers
    tickers = find_active_tickers()
    if not tickers:
        print("No active tickers found!")
        return

    for series, info in tickers.items():
        ticker = info["ticker"]
        print(f"\n\n{'#'*60}")
        print(f"# SERIES: {series} | TICKER: {ticker}")
        print(f"{'#'*60}")

        # 1. Full market object inspection
        market = inspect_market_fields(ticker)

        # 2. Try /trades endpoint
        probe("GET", f"{BASE}/markets/{ticker}/trades",
               params={"limit": 10},
               label=f"{series} /trades")

        # 3. Try /history endpoint
        probe("GET", f"{BASE}/markets/{ticker}/history",
               params={"limit": 10},
               label=f"{series} /history")

        # 4. Try /stats endpoint
        probe("GET", f"{BASE}/markets/{ticker}/stats",
               label=f"{series} /stats")

        # 5. Try /candlesticks (another common endpoint)
        probe("GET", f"{BASE}/markets/{ticker}/candlesticks",
               params={"period_interval": 60},
               label=f"{series} /candlesticks")

        # 6. Try series-level trades
        probe("GET", f"{BASE}/series/{series}/trades",
               params={"limit": 10},
               label=f"{series} series-level /trades")

        # 7. Try general /trades endpoint with ticker filter
        probe("GET", f"{BASE}/markets/trades",
               params={"ticker": ticker, "limit": 10},
               label=f"general /trades with ticker={ticker}")

        # 8. Try /exchange/schedule or similar metadata
        probe("GET", f"{BASE}/exchange/status",
               label="exchange status")

    # Step 2: Compare volume fields if found
    print(f"\n\n{'#'*60}")
    print("# COMPARISON SUMMARY")
    print(f"{'#'*60}")
    for series, info in tickers.items():
        print(f"\n{series}: {info['ticker']}")
        print(f"  Strike: {info['strike']}")
        print(f"  Close: {info['close_time']}")


if __name__ == "__main__":
    main()
