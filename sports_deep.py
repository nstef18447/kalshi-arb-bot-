"""Deep dive into sports market spreads and game-day markets."""
import time
from dotenv import load_dotenv
load_dotenv()
from auth import authenticated_request


def main():
    # Check orderbooks on game-day markets
    print("=== Game-day market samples ===")
    print()

    data = authenticated_request("GET", "/trade-api/v2/markets", params={
        "status": "open", "limit": 5, "category": "Sports",
    })
    markets = data.get("markets", [])

    for m in markets[:3]:
        ticker = m.get("ticker", "?")
        title = (m.get("subtitle", "") or m.get("title", "") or "?")[:70]
        print(f"Market: {ticker}")
        print(f"  Title: {title}")
        print(f"  Volume: {m.get('volume', 0)}  OI: {m.get('open_interest', 0)}")
        print(f"  Type: {m.get('market_type', '?')}  Tick: {m.get('tick_size', '?')}")
        print(f"  Fractional: {m.get('fractional_trading_enabled', '?')}")
        yb = m.get("yes_bid")
        ya = m.get("yes_ask")
        nb = m.get("no_bid")
        na = m.get("no_ask")
        print(f"  yes_bid={yb} yes_ask={ya} no_bid={nb} no_ask={na}")

        try:
            book = authenticated_request(
                "GET", f"/trade-api/v2/markets/{ticker}/orderbook",
                params={"depth": 3}
            )
            ob = book.get("orderbook", {})
            print(f"  Orderbook yes: {ob.get('yes', [])[:3]}")
            print(f"  Orderbook no:  {ob.get('no', [])[:3]}")
        except Exception as e:
            print(f"  Orderbook error: {e}")
        print()
        time.sleep(0.2)

    # Scan ALL sports markets for wide spreads
    print("=== Scanning ALL sports markets for wide spreads ===")
    print()

    all_markets = []
    cursor = None
    pages = 0
    while pages < 30:
        p = {"status": "open", "limit": 200, "category": "Sports"}
        if cursor:
            p["cursor"] = cursor
        data = authenticated_request("GET", "/trade-api/v2/markets", params=p)
        batch = data.get("markets", [])
        all_markets.extend(batch)
        cursor = data.get("cursor")
        pages += 1
        if not cursor or not batch:
            break

    print(f"Total sports markets fetched: {len(all_markets)}")

    # Filter for markets with bid/ask data
    has_book = 0
    wide = []
    for m in all_markets:
        yb = m.get("yes_bid")
        ya = m.get("yes_ask")
        if yb is not None and ya is not None and yb > 0 and ya > 0:
            has_book += 1
            spread = ya - yb
            if spread >= 3:
                wide.append((spread, m))

    wide.sort(key=lambda x: (-x[0], -(x[1].get("volume", 0) or 0)))

    print(f"Markets with any bid+ask: {has_book}")
    print(f"Markets with spread >= 3c: {len(wide)}")
    gt5 = sum(1 for s, _ in wide if s >= 5)
    gt10 = sum(1 for s, _ in wide if s >= 10)
    print(f"Markets with spread >= 5c: {gt5}")
    print(f"Markets with spread >= 10c: {gt10}")
    print()

    if wide:
        print("Top 30 widest-spread sports markets:")
        print(f"{'Spread':>6}  {'Volume':>10}  {'Bid':>4}  {'Ask':>4}  {'OI':>8}  Title")
        print("-" * 90)
        for spread, m in wide[:30]:
            title = (m.get("subtitle", "") or m.get("title", "") or "?")[:50]
            vol = m.get("volume", 0) or 0
            yb = m.get("yes_bid", 0)
            ya = m.get("yes_ask", 0)
            oi = m.get("open_interest", 0) or 0
            ticker = m.get("ticker", "?")
            print(f"{spread:>5}c  {vol:>10,}  {yb:>4}  {ya:>4}  {oi:>8,}  {title}")
        print()

    # Now check OTHER categories too — what has the widest spreads on all of Kalshi?
    print("=== Widest spreads across ALL categories ===")
    print()
    all_cats = []
    for cat in ["Economics", "Crypto", "Financials", "Politics",
                "Entertainment", "Climate and Weather", "Elections"]:
        cursor = None
        pages = 0
        while pages < 5:
            p = {"status": "open", "limit": 200, "category": cat}
            if cursor:
                p["cursor"] = cursor
            data = authenticated_request("GET", "/trade-api/v2/markets", params=p)
            batch = data.get("markets", [])
            for m in batch:
                m["_cat"] = cat
            all_cats.extend(batch)
            cursor = data.get("cursor")
            pages += 1
            if not cursor or not batch:
                break

    print(f"Non-sports markets fetched: {len(all_cats)}")

    wide_all = []
    for m in all_cats:
        yb = m.get("yes_bid")
        ya = m.get("yes_ask")
        if yb is not None and ya is not None and yb > 0 and ya > 0:
            spread = ya - yb
            vol = m.get("volume", 0) or 0
            if spread >= 5 and vol > 0:
                wide_all.append((spread, vol, m))

    wide_all.sort(key=lambda x: (-x[1], -x[0]))  # Sort by volume desc
    print(f"Non-sports markets with spread >= 5c and volume > 0: {len(wide_all)}")
    print()

    if wide_all:
        print("Top 30 by volume (spread >= 5c):")
        print(f"{'Cat':<15} {'Spread':>6} {'Volume':>10} {'Bid':>4} {'Ask':>4} {'OI':>8}  Title")
        print("-" * 100)
        for spread, vol, m in wide_all[:30]:
            cat = m.get("_cat", "?")[:14]
            title = (m.get("subtitle", "") or m.get("title", "") or "?")[:45]
            yb = m.get("yes_bid", 0)
            ya = m.get("yes_ask", 0)
            oi = m.get("open_interest", 0) or 0
            ticker = m.get("ticker", "?")
            series = m.get("series_ticker", "?")
            print(f"{cat:<15} {spread:>5}c {vol:>10,} {yb:>4} {ya:>4} {oi:>8,}  {title}  [{series}]")


if __name__ == "__main__":
    main()
