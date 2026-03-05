"""Search for 15-minute BTC contracts on Kalshi."""
from datetime import datetime
from dotenv import load_dotenv
load_dotenv()
import kalshi_api
from auth import authenticated_request

def parse_ts(s):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except:
        return None

# ── 1. Try candidate series tickers ──
print("=" * 70)
print("STEP 1: Try candidate series tickers")
print("=" * 70)

candidates = ["KXBTC", "KXBTC15", "KXBTC15M", "KXBTCU", "KXBTCF",
              "KXBTCD", "KXBTCW", "KXBTCH", "KXBTCM"]
for ticker in candidates:
    try:
        events = kalshi_api.get_events(ticker, "open")
        if events:
            print(f"\n  {ticker}: {len(events)} open events")
            for ev in events[:3]:
                et = ev.get("event_ticker", "")
                title = ev.get("title", "")
                markets = kalshi_api.get_markets_for_event(et, "open")
                duration_min = None
                if markets:
                    m = markets[0]
                    ot = parse_ts(m.get("open_time", ""))
                    ct = parse_ts(m.get("close_time", ""))
                    if ot and ct:
                        duration_min = (ct - ot).total_seconds() / 60
                dur_str = f"{duration_min:.0f}min" if duration_min else "???"
                print(f"    {et}: {title} [{len(markets)} mkts, {dur_str}]")
        else:
            print(f"  {ticker}: 0 events")
    except Exception as e:
        print(f"  {ticker}: ERROR - {e}")

# ── 2. Search crypto category for anything with "15" or "minute" ──
print("\n" + "=" * 70)
print("STEP 2: Search crypto category events")
print("=" * 70)

try:
    # Paginate through all crypto events
    all_events = []
    cursor = None
    while True:
        params = {"status": "open", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        data = authenticated_request("GET", "/trade-api/v2/events", params=params)
        events = data.get("events", [])
        all_events.extend(events)
        cursor = data.get("cursor")
        if not cursor or not events:
            break

    print(f"  Total open events across all categories: {len(all_events)}")

    # Filter for BTC/bitcoin related
    btc_events = [e for e in all_events if any(
        kw in (e.get("title", "") + " " + e.get("event_ticker", "")).lower()
        for kw in ["btc", "bitcoin"]
    )]
    print(f"  BTC-related events: {len(btc_events)}")
    for ev in btc_events:
        et = ev.get("event_ticker", "")
        title = ev.get("title", "")
        series = ev.get("series_ticker", "")
        print(f"    [{series}] {et}: {title}")

    # Filter for anything mentioning 15/fifteen/minute
    minute_events = [e for e in all_events if any(
        kw in e.get("title", "").lower()
        for kw in ["15", "fifteen", "minute", "15-min", "15min"]
    )]
    print(f"\n  Events with '15/fifteen/minute' in title: {len(minute_events)}")
    for ev in minute_events:
        et = ev.get("event_ticker", "")
        title = ev.get("title", "")
        series = ev.get("series_ticker", "")
        print(f"    [{series}] {et}: {title}")

except Exception as e:
    print(f"  ERROR: {e}")

# ── 3. Check ALL distinct durations under KXBTC ──
print("\n" + "=" * 70)
print("STEP 3: All distinct event durations under KXBTC")
print("=" * 70)

events = kalshi_api.get_events("KXBTC", "open")
# Also check recently closed
try:
    closed_events = kalshi_api.get_events("KXBTC", "closed")
    print(f"  Open events: {len(events)}, Recently closed events: {len(closed_events)}")
except:
    closed_events = []
    print(f"  Open events: {len(events)}, Could not fetch closed events")

all_kxbtc = events + closed_events[:10]  # sample of closed too

durations_seen = {}
for ev in all_kxbtc:
    et = ev.get("event_ticker", "")
    title = ev.get("title", "")
    try:
        markets = kalshi_api.get_markets_for_event(et, "open")
        if not markets:
            markets = kalshi_api.get_markets_for_event(et, "closed")
        if markets:
            m = markets[0]
            ot = parse_ts(m.get("open_time", ""))
            ct = parse_ts(m.get("close_time", ""))
            if ot and ct:
                dur = (ct - ot).total_seconds() / 60
                dur_key = f"{dur:.0f}min"
                if dur_key not in durations_seen:
                    durations_seen[dur_key] = []
                durations_seen[dur_key].append((et, title, len(markets)))
    except Exception as e:
        print(f"  {et}: ERROR - {e}")

print(f"\n  Distinct durations found: {len(durations_seen)}")
for dur, examples in sorted(durations_seen.items(), key=lambda x: float(x[0].replace("min",""))):
    print(f"\n  {dur} ({float(dur.replace('min',''))/60:.1f}h):")
    for et, title, n_mkts in examples[:3]:
        print(f"    {et}: {title} [{n_mkts} mkts]")

# ── 4. Try fetching series list directly ──
print("\n" + "=" * 70)
print("STEP 4: All available series")
print("=" * 70)

try:
    data = authenticated_request("GET", "/trade-api/v2/series", params={})
    series_list = data.get("series", [])
    print(f"  Total series: {len(series_list)}")
    # Filter for crypto-related
    crypto_series = [s for s in series_list if any(
        kw in (s.get("ticker", "") + " " + s.get("title", "")).lower()
        for kw in ["btc", "bitcoin", "eth", "ethereum", "sol", "crypto", "kxbtc", "kxeth", "kxsol"]
    )]
    print(f"  Crypto-related series: {len(crypto_series)}")
    for s in crypto_series:
        print(f"    {s.get('ticker', '???')}: {s.get('title', '???')}")
        print(f"      keys: {sorted(s.keys())}")
except Exception as e:
    print(f"  Series endpoint error: {e}")
    # Try alternate
    try:
        data = authenticated_request("GET", "/trade-api/v2/series/KXBTC", params={})
        print(f"  KXBTC series info: {data}")
    except Exception as e2:
        print(f"  KXBTC direct lookup error: {e2}")
