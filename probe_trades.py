"""Probe trade history for key markets."""
import sys, json, time, os, base64, requests
from collections import defaultdict
from datetime import datetime
sys.path.insert(0, "/opt/kalshi-arb-bot")
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

key_path = os.environ["KALSHI_PRIVATE_KEY_PATH"]
api_key = os.environ["KALSHI_API_KEY"]
base_url = "https://api.elections.kalshi.com"

with open(key_path, "rb") as f:
    priv_key = serialization.load_pem_private_key(f.read(), password=None)

def call(method, path, params=None):
    ts = int(time.time() * 1000)
    msg = f"{ts}{method}{path}".encode()
    sig = base64.b64encode(priv_key.sign(
        msg, padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                         salt_length=padding.PSS.MAX_LENGTH),
        hashes.SHA256()
    )).decode()
    headers = {
        "KALSHI-ACCESS-KEY": api_key,
        "KALSHI-ACCESS-SIGNATURE": sig,
        "KALSHI-ACCESS-TIMESTAMP": str(ts),
        "Content-Type": "application/json",
    }
    r = requests.request(method, base_url + path, headers=headers, params=params, timeout=10)
    return r.status_code, r.json() if r.status_code == 200 else {}

def fetch_all_trades(ticker, max_pages=50):
    all_trades = []
    cursor = None
    for _ in range(max_pages):
        params = {"ticker": ticker, "limit": 100}
        if cursor:
            params["cursor"] = cursor
        code, data = call("GET", "/trade-api/v2/markets/trades", params)
        if code != 200:
            break
        trades = data.get("trades", [])
        all_trades.extend(trades)
        cursor = data.get("cursor")
        if not cursor or not trades:
            break
    return all_trades

def analyze_trades(ticker, label):
    print(f"\n{'='*60}")
    print(f"TRADE ANALYSIS: {label}")
    print(f"Ticker: {ticker}")
    print(f"{'='*60}")

    trades = fetch_all_trades(ticker)
    print(f"Total trades fetched: {len(trades)}")

    if not trades:
        print("No trades found.")
        return {"trades": 0, "contracts": 0, "trades_per_hr": 0}

    earliest = trades[-1]["created_time"]
    latest = trades[0]["created_time"]
    print(f"Earliest: {earliest}")
    print(f"Latest: {latest}")

    # Trades per hour
    hourly = defaultdict(lambda: {"trades": 0, "contracts": 0})
    for t in trades:
        hr = t["created_time"][:13]
        hourly[hr]["trades"] += 1
        hourly[hr]["contracts"] += t.get("count", 1)

    print(f"\nTrades per hour:")
    for hr in sorted(hourly.keys()):
        h = hourly[hr]
        print(f"  {hr}: {h['trades']} trades, {h['contracts']:,} contracts")

    # Taker side
    yes_takers = sum(1 for t in trades if t.get("taker_side") == "yes")
    no_takers = sum(1 for t in trades if t.get("taker_side") == "no")
    total_contracts = sum(t.get("count", 1) for t in trades)
    print(f"\nTaker side: yes={yes_takers} ({100*yes_takers/len(trades):.0f}%), no={no_takers} ({100*no_takers/len(trades):.0f}%)")
    print(f"Total contracts traded: {total_contracts:,}")

    # Contract size distribution
    counts = [t.get("count", 1) for t in trades]
    counts.sort()
    print(f"\nContract size per trade:")
    print(f"  Min: {min(counts)}, Median: {counts[len(counts)//2]}, Max: {max(counts)}")
    print(f"  Size 1: {sum(1 for c in counts if c == 1)} trades")
    print(f"  Size 2-10: {sum(1 for c in counts if 2 <= c <= 10)} trades")
    print(f"  Size 11-100: {sum(1 for c in counts if 11 <= c <= 100)} trades")
    print(f"  Size 100+: {sum(1 for c in counts if c > 100)} trades")

    # Price distribution
    prices = [t.get("yes_price", 0) for t in trades]
    print(f"\nPrice range: {min(prices)}-{max(prices)}c")

    # Time between trades
    times = []
    for t in trades:
        ts_str = t["created_time"].replace("Z", "+00:00")
        times.append(datetime.fromisoformat(ts_str))
    times.sort()

    gaps = [(times[i+1] - times[i]).total_seconds() for i in range(len(times)-1)]
    if gaps:
        gaps.sort()
        total_hours = (times[-1] - times[0]).total_seconds() / 3600
        print(f"\nTime span: {total_hours:.1f} hours")
        print(f"Trades/hour: {len(trades)/max(0.1, total_hours):.1f}")
        print(f"\nTime between trades:")
        print(f"  Min: {gaps[0]:.0f}s")
        print(f"  P25: {gaps[len(gaps)//4]:.0f}s")
        print(f"  Median: {gaps[len(gaps)//2]:.0f}s")
        print(f"  P75: {gaps[3*len(gaps)//4]:.0f}s")
        print(f"  Max: {gaps[-1]:.0f}s")
        print(f"  < 10s: {sum(1 for g in gaps if g < 10)} ({100*sum(1 for g in gaps if g < 10)/len(gaps):.0f}%)")
        print(f"  < 60s: {sum(1 for g in gaps if g < 60)} ({100*sum(1 for g in gaps if g < 60)/len(gaps):.0f}%)")
        print(f"  < 300s: {sum(1 for g in gaps if g < 300)} ({100*sum(1 for g in gaps if g < 300)/len(gaps):.0f}%)")
        return {"trades": len(trades), "contracts": total_contracts, "trades_per_hr": len(trades)/max(0.1, total_hours)}

    return {"trades": len(trades), "contracts": total_contracts, "trades_per_hr": 0}


def main():
    # Key tickers to analyze (high volume from our earlier probe)
    tickers = [
        ("KXBTCD-26FEB2717-T68499.99", "KXBTCD ATM (68.5k, daily)"),
        ("KXBTCD-26FEB2717-T67999.99", "KXBTCD near-ATM (68k, daily)"),
        ("KXBTCD-26FEB2717-T69999.99", "KXBTCD OTM (70k, daily)"),
        ("KXETHD-26FEB2717-T2029.99",  "KXETHD top volume (2030, daily)"),
        ("KXSOLD-26FEB2717-T80.9999",  "KXSOLD top volume (81, daily)"),
        # Range comparison
        ("KXBTC-26FEB2717-B67750",     "KXBTC range (67750, daily)"),
    ]

    results = {}
    for ticker, label in tickers:
        results[label] = analyze_trades(ticker, label)

    # Summary
    print(f"\n\n{'='*60}")
    print("SUMMARY")
    print(f"{'='*60}")
    print(f"{'Market':<40} {'Trades':>8} {'Contracts':>12} {'Trades/Hr':>12}")
    print("-" * 75)
    for label, r in results.items():
        print(f"{label:<40} {r['trades']:>8} {r['contracts']:>12,} {r['trades_per_hr']:>12.1f}")


if __name__ == "__main__":
    main()
