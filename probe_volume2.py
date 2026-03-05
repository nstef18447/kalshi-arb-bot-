"""Probe Kalshi API for volume/trade data — focused version."""
import sys, os, json, time
sys.path.insert(0, "/opt/kalshi-arb-bot")
from auth import authenticated_request

BASE = "/trade-api/v2"

def get_markets(series, limit=200):
    """Get all open markets for a series."""
    all_markets = []
    cursor = None
    while True:
        params = {"series_ticker": series, "status": "open", "limit": limit}
        if cursor:
            params["cursor"] = cursor
        data = authenticated_request("GET", f"{BASE}/markets", params=params)
        markets = data.get("markets", [])
        all_markets.extend(markets)
        cursor = data.get("cursor")
        if not cursor or not markets:
            break
    return all_markets

def try_endpoint(method, path, params=None):
    """Try an endpoint, return (status_code, data)."""
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
        if resp.status_code == 200:
            return resp.status_code, resp.json()
        return resp.status_code, resp.text[:200]
    except Exception as e:
        return 0, str(e)

def main():
    print("=" * 70)
    print("KALSHI VOLUME & TRADE DATA PROBE")
    print("=" * 70)

    # ===============================================
    # PART 1: Dump all fields from one market object
    # ===============================================
    print("\n## PART 1: Full Market Object Fields")
    data = authenticated_request("GET", f"{BASE}/markets",
                                  params={"series_ticker": "KXBTCD", "status": "open", "limit": 1})
    if data.get("markets"):
        m = data["markets"][0]
        print(f"\nTicker: {m.get('ticker')}")
        print(f"ALL FIELDS ({len(m)} keys):")
        for k in sorted(m.keys()):
            print(f"  {k}: {m[k]}")

        # Highlight volume fields
        vol_fields = {k: m[k] for k in m if any(t in k.lower() for t in
                      ["volume", "trade", "interest", "last", "liquidity", "notional", "count"])}
        print(f"\nVOLUME-RELATED FIELDS:")
        for k, v in sorted(vol_fields.items()):
            print(f"  ** {k}: {v} **")

    # ===============================================
    # PART 2: Volume/OI comparison across series
    # ===============================================
    print("\n\n## PART 2: Volume & OI Across Series")

    series_stats = {}
    for series in ["KXBTCD", "KXETHD", "KXSOLD", "KXBTC", "KXETH", "KXSOLE"]:
        print(f"\nFetching {series}...")
        markets = get_markets(series)
        print(f"  {len(markets)} open markets")

        total_volume = 0
        total_oi = 0
        total_volume_24h = 0
        markets_with_volume = 0
        markets_with_oi = 0
        best_volume = None

        for m in markets:
            vol = m.get("volume", 0) or 0
            vol_24h = m.get("volume_24h", 0) or 0
            oi = m.get("open_interest", 0) or 0

            total_volume += vol
            total_volume_24h += vol_24h
            total_oi += oi
            if vol > 0:
                markets_with_volume += 1
            if oi > 0:
                markets_with_oi += 1
            if best_volume is None or vol > best_volume.get("volume", 0):
                best_volume = m

        series_stats[series] = {
            "count": len(markets),
            "total_volume": total_volume,
            "total_volume_24h": total_volume_24h,
            "total_oi": total_oi,
            "markets_with_volume": markets_with_volume,
            "markets_with_oi": markets_with_oi,
            "best_volume_ticker": best_volume.get("ticker") if best_volume else "N/A",
            "best_volume": best_volume.get("volume", 0) if best_volume else 0,
            "best_oi": best_volume.get("open_interest", 0) if best_volume else 0,
        }

        print(f"  Total volume: {total_volume:,}")
        print(f"  Total volume_24h: {total_volume_24h:,}")
        print(f"  Total open_interest: {total_oi:,}")
        print(f"  Markets with volume > 0: {markets_with_volume}")
        print(f"  Markets with OI > 0: {markets_with_oi}")
        if best_volume:
            print(f"  Highest volume market: {best_volume.get('ticker')} vol={best_volume.get('volume',0):,} OI={best_volume.get('open_interest',0):,}")

    # Summary table
    print("\n\n## SUMMARY TABLE")
    print(f"{'Series':<15} {'Markets':>8} {'Total Vol':>12} {'Vol 24h':>12} {'Total OI':>12} {'w/ Vol':>8} {'w/ OI':>8}")
    print("-" * 85)
    for series in ["KXBTCD", "KXETHD", "KXSOLD", "KXBTC", "KXETH", "KXSOLE"]:
        s = series_stats[series]
        print(f"{series:<15} {s['count']:>8} {s['total_volume']:>12,} {s['total_volume_24h']:>12,} {s['total_oi']:>12,} {s['markets_with_volume']:>8} {s['markets_with_oi']:>8}")

    # ===============================================
    # PART 3: Try trade history endpoints
    # ===============================================
    print("\n\n## PART 3: Trade History Endpoints")

    # Pick one active ticker from KXBTCD
    kxbtcd_markets = get_markets("KXBTCD")
    # Find one with highest OI
    kxbtcd_markets.sort(key=lambda m: m.get("open_interest", 0), reverse=True)
    test_ticker = kxbtcd_markets[0]["ticker"] if kxbtcd_markets else None

    if test_ticker:
        print(f"\nTest ticker: {test_ticker} (OI: {kxbtcd_markets[0].get('open_interest',0)})")

        endpoints = [
            (f"{BASE}/markets/{test_ticker}/trades", {"limit": 10}, "market trades"),
            (f"{BASE}/markets/{test_ticker}/history", {"limit": 10}, "market history"),
            (f"{BASE}/markets/{test_ticker}/stats", {}, "market stats"),
            (f"{BASE}/markets/{test_ticker}/candlesticks", {"period_interval": 60}, "candlesticks"),
            (f"{BASE}/markets/trades", {"ticker": test_ticker, "limit": 10}, "global trades filtered"),
            (f"{BASE}/trades", {"ticker": test_ticker, "limit": 10}, "root trades"),
        ]

        for path, params, label in endpoints:
            code, data = try_endpoint("GET", path, params)
            print(f"\n  {label}: {path}")
            print(f"    Status: {code}")
            if code == 200 and isinstance(data, dict):
                txt = json.dumps(data, indent=2)[:1500]
                for line in txt.split("\n"):
                    print(f"    {line}")
            else:
                print(f"    Response: {str(data)[:300]}")

    # ===============================================
    # PART 4: Per-expiry volume comparison (KXBTCD vs KXBTC)
    # ===============================================
    print("\n\n## PART 4: KXBTCD vs KXBTC — Same Expiry Volume Comparison")

    # Group by close_time
    from collections import defaultdict
    kxbtcd_by_close = defaultdict(list)
    kxbtc_by_close = defaultdict(list)

    for m in get_markets("KXBTCD"):
        ct = m.get("close_time", "")[:10]  # date only
        kxbtcd_by_close[ct].append(m)
    for m in get_markets("KXBTC"):
        ct = m.get("close_time", "")[:10]
        kxbtc_by_close[ct].append(m)

    common_dates = sorted(set(kxbtcd_by_close.keys()) & set(kxbtc_by_close.keys()))

    print(f"\nCommon close dates: {common_dates[:5]}")
    for dt in common_dates[:3]:
        d_markets = kxbtcd_by_close[dt]
        r_markets = kxbtc_by_close[dt]
        d_vol = sum(m.get("volume", 0) or 0 for m in d_markets)
        d_oi = sum(m.get("open_interest", 0) or 0 for m in d_markets)
        r_vol = sum(m.get("volume", 0) or 0 for m in r_markets)
        r_oi = sum(m.get("open_interest", 0) or 0 for m in r_markets)
        print(f"\n  Close date: {dt}")
        print(f"    KXBTCD: {len(d_markets)} markets, volume={d_vol:,}, OI={d_oi:,}")
        print(f"    KXBTC:  {len(r_markets)} markets, volume={r_vol:,}, OI={r_oi:,}")

    # ===============================================
    # PART 5: Top 20 most traded markets across all AB series
    # ===============================================
    print("\n\n## PART 5: Top 20 Most Traded Above/Below Markets")

    all_ab = []
    for series in ["KXBTCD", "KXETHD", "KXSOLD"]:
        for m in get_markets(series):
            all_ab.append(m)

    all_ab.sort(key=lambda m: m.get("volume", 0) or 0, reverse=True)

    print(f"\n{'Ticker':<35} {'Strike':>10} {'Volume':>10} {'OI':>10} {'Last':>6} {'Close Time':<22}")
    print("-" * 100)
    for m in all_ab[:20]:
        print(f"{m.get('ticker',''):<35} {m.get('floor_strike',0):>10.2f} {m.get('volume',0) or 0:>10,} {m.get('open_interest',0) or 0:>10,} {m.get('last_price',0) or 0:>6} {m.get('close_time','')[:22]}")

    # Also show distribution
    volumes = [m.get("volume", 0) or 0 for m in all_ab]
    vol_zero = sum(1 for v in volumes if v == 0)
    vol_1_10 = sum(1 for v in volumes if 1 <= v <= 10)
    vol_11_100 = sum(1 for v in volumes if 11 <= v <= 100)
    vol_101_1000 = sum(1 for v in volumes if 101 <= v <= 1000)
    vol_1000_plus = sum(1 for v in volumes if v > 1000)

    print(f"\nVolume distribution ({len(all_ab)} markets):")
    print(f"  Volume = 0:      {vol_zero}")
    print(f"  Volume 1-10:     {vol_1_10}")
    print(f"  Volume 11-100:   {vol_11_100}")
    print(f"  Volume 101-1000: {vol_101_1000}")
    print(f"  Volume 1000+:    {vol_1000_plus}")

if __name__ == "__main__":
    main()
