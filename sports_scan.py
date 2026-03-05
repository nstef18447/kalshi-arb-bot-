"""Sports market feasibility scan for Kalshi market making.

Queries the Kalshi API to discover sports markets, analyze spreads,
volume, trade frequency, and fee structure. Outputs a markdown report.
"""

import os
import sys
import time
import statistics
from collections import defaultdict
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()

from auth import authenticated_request


def paginate(path, params, items_key):
    all_items = []
    cursor = None
    while True:
        p = dict(params)
        if cursor:
            p["cursor"] = cursor
        data = authenticated_request("GET", path, params=p)
        items = data.get(items_key, [])
        all_items.extend(items)
        cursor = data.get("cursor")
        if not cursor or not items:
            break
    return all_items


def get_orderbook(ticker, depth=3):
    data = authenticated_request(
        "GET", f"/trade-api/v2/markets/{ticker}/orderbook",
        params={"depth": depth}
    )
    return data.get("orderbook", {})


def get_trades(ticker, limit=100):
    """Fetch recent trades for a market."""
    data = authenticated_request(
        "GET", f"/trade-api/v2/markets/{ticker}/trades",
        params={"limit": limit}
    )
    return data.get("trades", [])


def best_bid(levels):
    if not levels:
        return None
    prices = [l[0] for l in levels if isinstance(l, list)]
    return max(prices) if prices else None


def total_depth(levels):
    if not levels:
        return 0
    return sum(l[1] for l in levels if isinstance(l, list) and len(l) >= 2)


def fmt_spread(yb, nb):
    if yb is None or nb is None:
        return None, "?"
    spread = (100 - nb) - yb
    return spread, f"{spread}c"


out = []


def p(line=""):
    out.append(line)
    print(line)


def main():
    p("# Kalshi Sports Market Feasibility Scan")
    p(f"**Date**: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    p()

    # ================================================================
    # SECTION 1: Discover sports events
    # ================================================================
    p("## 1. Discovering Sports Events")
    p()

    # Try fetching events with various category/search params
    # Kalshi v2 API: GET /trade-api/v2/events supports status, series_ticker,
    # and potentially category. Let's try broad searches.

    sport_keywords = ["NBA", "NFL", "NHL", "MLB", "NCAA", "soccer", "tennis",
                      "MMA", "UFC", "boxing", "golf", "PGA", "WNBA", "MLS",
                      "Premier League", "Champions League", "football", "basketball",
                      "hockey", "baseball"]

    # First: try to get ALL active events and filter
    p("### Fetching all active events...")
    all_events = paginate("/trade-api/v2/events", {"status": "open", "limit": 200}, "events")
    p(f"Total active events: **{len(all_events)}**")
    p()

    # Categorize events
    sports_events = []
    crypto_events = []
    other_events = []

    # Build a map of category -> events
    category_map = defaultdict(list)
    for ev in all_events:
        cat = ev.get("category", "unknown")
        category_map[cat].append(ev)

        title = (ev.get("title", "") or "").lower()
        series = (ev.get("series_ticker", "") or "").lower()
        tags = " ".join(str(t) for t in ev.get("tags", []) if t).lower() if ev.get("tags") else ""

        combined = f"{title} {series} {tags}"

        is_sport = any(kw.lower() in combined for kw in sport_keywords)
        is_crypto = any(kw in combined for kw in ["btc", "bitcoin", "eth", "ethereum",
                                                    "sol", "solana", "crypto", "kxbtc",
                                                    "kxeth", "kxsol"])

        if is_sport:
            sports_events.append(ev)
        elif is_crypto:
            crypto_events.append(ev)
        else:
            other_events.append(ev)

    p("### Events by category field:")
    for cat in sorted(category_map.keys()):
        evts = category_map[cat]
        p(f"- **{cat}**: {len(evts)} events")
    p()

    p(f"### Keyword classification:")
    p(f"- Sports: **{len(sports_events)}** events")
    p(f"- Crypto: **{len(crypto_events)}** events")
    p(f"- Other: **{len(other_events)}** events")
    p()

    # Show sample sports events
    if sports_events:
        p("### Sample sports events:")
        p("| Event Ticker | Title | Series | Category | Markets |")
        p("|---|---|---|---|---|")
        for ev in sports_events[:30]:
            ticker = ev.get("event_ticker", "?")
            title = (ev.get("title", "?") or "?")[:60]
            series = ev.get("series_ticker", "?")
            cat = ev.get("category", "?")
            n_markets = ev.get("markets_count", ev.get("num_markets", "?"))
            # Try to get market count if not in event data
            if n_markets == "?":
                n_markets = len(ev.get("markets", []))
            p(f"| {ticker} | {title} | {series} | {cat} | {n_markets} |")
        if len(sports_events) > 30:
            p(f"| ... | *{len(sports_events) - 30} more* | | | |")
        p()

    # Also show "other" events that might be sports
    if other_events:
        p("### 'Other' events (may contain sports):")
        p("| Event Ticker | Title | Series | Category |")
        p("|---|---|---|---|")
        for ev in other_events[:20]:
            ticker = ev.get("event_ticker", "?")
            title = (ev.get("title", "?") or "?")[:70]
            series = ev.get("series_ticker", "?")
            cat = ev.get("category", "?")
            p(f"| {ticker} | {title} | {series} | {cat} |")
        if len(other_events) > 20:
            p(f"| ... | *{len(other_events) - 20} more* | | |")
        p()

    # ================================================================
    # SECTION 2: Find multi-strike sports markets
    # ================================================================
    p("## 2. Multi-Strike Sports Markets")
    p()

    # For events with many markets, fetch their market list
    multi_contract_events = []

    # Check all sports events for market counts
    for ev in sports_events:
        n = ev.get("markets_count", ev.get("num_markets", 0))
        if isinstance(n, int) and n >= 5:
            multi_contract_events.append(ev)

    # If market counts aren't in event data, probe a sample
    if not multi_contract_events:
        p("Market counts not in event data — probing events for market lists...")
        p()
        for ev in sports_events[:20]:
            evt_ticker = ev.get("event_ticker", "")
            if not evt_ticker:
                continue
            try:
                markets = paginate(
                    "/trade-api/v2/markets",
                    {"event_ticker": evt_ticker, "limit": 200},
                    "markets",
                )
                ev["_markets"] = markets
                ev["_n_markets"] = len(markets)
                if len(markets) >= 5:
                    multi_contract_events.append(ev)
            except Exception as e:
                ev["_n_markets"] = f"error: {e}"
            time.sleep(0.1)  # rate limit courtesy

    # Also probe "other" events (many sports might be miscategorized)
    for ev in other_events[:30]:
        evt_ticker = ev.get("event_ticker", "")
        if not evt_ticker:
            continue
        try:
            markets = paginate(
                "/trade-api/v2/markets",
                {"event_ticker": evt_ticker, "limit": 200},
                "markets",
            )
            ev["_markets"] = markets
            ev["_n_markets"] = len(markets)
            if len(markets) >= 5:
                multi_contract_events.append(ev)
        except Exception as e:
            ev["_n_markets"] = f"error: {e}"
        time.sleep(0.1)

    if multi_contract_events:
        p(f"### Events with 5+ contracts: **{len(multi_contract_events)}**")
        p()
        p("| Event | Title | #Markets | Sample Market Titles |")
        p("|---|---|---|---|")
        for ev in sorted(multi_contract_events, key=lambda e: e.get("_n_markets", 0), reverse=True)[:25]:
            ticker = ev.get("event_ticker", "?")
            title = (ev.get("title", "?") or "?")[:50]
            n = ev.get("_n_markets", "?")
            mkts = ev.get("_markets", [])
            samples = "; ".join((m.get("title", "") or m.get("subtitle", "") or "?")[:40] for m in mkts[:3])
            p(f"| {ticker} | {title} | {n} | {samples} |")
        p()
    else:
        p("No multi-contract sports events found.")
        p()

    # ================================================================
    # SECTION 3: Single-outcome market analysis — top volume markets
    # ================================================================
    p("## 3. Spread & Volume Analysis — Top Markets")
    p()

    # Gather ALL markets from sports events (and other events) to find highest volume
    all_sports_markets = []
    events_to_probe = sports_events + other_events

    # We already probed some — collect those markets
    for ev in events_to_probe:
        if "_markets" in ev:
            for m in ev["_markets"]:
                m["_event_title"] = ev.get("title", "")
                all_sports_markets.append(m)

    # Probe remaining sports events we haven't fetched yet
    probed_events = {ev.get("event_ticker") for ev in events_to_probe if "_markets" in ev}
    remaining = [ev for ev in sports_events if ev.get("event_ticker") not in probed_events]

    for ev in remaining[:30]:
        evt_ticker = ev.get("event_ticker", "")
        if not evt_ticker:
            continue
        try:
            markets = paginate(
                "/trade-api/v2/markets",
                {"event_ticker": evt_ticker, "limit": 200},
                "markets",
            )
            for m in markets:
                m["_event_title"] = ev.get("title", "")
            all_sports_markets.extend(markets)
        except Exception:
            pass
        time.sleep(0.1)

    p(f"Total markets scanned: **{len(all_sports_markets)}**")
    p()

    # Sort by volume (try different field names)
    for m in all_sports_markets:
        vol = m.get("volume", 0) or m.get("volume_24h", 0) or 0
        m["_vol"] = vol

    by_volume = sorted(all_sports_markets, key=lambda m: m["_vol"], reverse=True)

    # Top 20 by volume — fetch orderbooks
    p("### Top 20 markets by volume:")
    p()
    p("| # | Ticker | Title | Vol | OI | Bid | Ask | Spread | YesD | NoD |")
    p("|---|---|---|---|---|---|---|---|---|---|")

    spread_counts = {"gt3": 0, "gt5": 0, "gt10": 0}
    vol_gt_10k = 0
    top_markets_detail = []

    for i, m in enumerate(by_volume[:20]):
        ticker = m.get("ticker", "?")
        title = (m.get("title", "") or m.get("subtitle", "") or "?")[:45]
        vol = m["_vol"]
        oi = m.get("open_interest", 0) or 0

        if vol > 10000:
            vol_gt_10k += 1

        # Fetch orderbook
        try:
            book = get_orderbook(ticker, depth=3)
            yb = best_bid(book.get("yes", []))
            nb = best_bid(book.get("no", []))
            yd = total_depth(book.get("yes", []))
            nd = total_depth(book.get("no", []))
            spread, spread_str = fmt_spread(yb, nb)

            if spread is not None:
                if spread > 3:
                    spread_counts["gt3"] += 1
                if spread > 5:
                    spread_counts["gt5"] += 1
                if spread > 10:
                    spread_counts["gt10"] += 1

            top_markets_detail.append({
                "ticker": ticker, "title": title, "vol": vol,
                "yb": yb, "nb": nb, "spread": spread, "yd": yd, "nd": nd,
            })

            yb_str = str(yb) if yb is not None else "—"
            nb_str = f"{100-nb}" if nb is not None else "—"
            p(f"| {i+1} | `{ticker}` | {title} | {vol:,} | {oi:,} | {yb_str} | {nb_str} | {spread_str} | {yd} | {nd} |")
        except Exception as e:
            p(f"| {i+1} | `{ticker}` | {title} | {vol:,} | {oi:,} | err | err | err | — | — |")

        time.sleep(0.15)

    p()
    p(f"**Spread distribution** (top 20 by volume):")
    p(f"- Spread > 3c: **{spread_counts['gt3']}** markets")
    p(f"- Spread > 5c: **{spread_counts['gt5']}** markets")
    p(f"- Spread > 10c: **{spread_counts['gt10']}** markets")
    p(f"- Volume > 10,000: **{vol_gt_10k}** markets")
    p()

    # Also scan a wider set for spread analysis
    p("### Wider spread scan (top 50 by volume):")
    wide_spreads = {"gt3": 0, "gt5": 0, "gt10": 0, "total": 0, "empty": 0}

    for m in by_volume[20:50]:
        ticker = m.get("ticker", "?")
        try:
            book = get_orderbook(ticker, depth=1)
            yb = best_bid(book.get("yes", []))
            nb = best_bid(book.get("no", []))
            spread, _ = fmt_spread(yb, nb)
            if spread is not None:
                wide_spreads["total"] += 1
                if spread > 3:
                    wide_spreads["gt3"] += 1
                if spread > 5:
                    wide_spreads["gt5"] += 1
                if spread > 10:
                    wide_spreads["gt10"] += 1
            else:
                wide_spreads["empty"] += 1
        except Exception:
            wide_spreads["empty"] += 1
        time.sleep(0.1)

    # Combine with top 20
    total_scanned = 20 + wide_spreads["total"]
    p(f"Combined top 50 (where orderbook exists): **{total_scanned}** markets")
    p(f"- Spread > 3c: **{spread_counts['gt3'] + wide_spreads['gt3']}**")
    p(f"- Spread > 5c: **{spread_counts['gt5'] + wide_spreads['gt5']}**")
    p(f"- Spread > 10c: **{spread_counts['gt10'] + wide_spreads['gt10']}**")
    p(f"- Empty/no orderbook: **{wide_spreads['empty']}**")
    p()

    # ================================================================
    # SECTION 4: Trade frequency for top markets
    # ================================================================
    p("## 4. Trade Frequency Analysis")
    p()

    # Pick top 3 most active markets with actual spreads
    active_markets = [m for m in top_markets_detail if m.get("spread") is not None and m["vol"] > 0][:5]

    for m in active_markets[:3]:
        ticker = m["ticker"]
        p(f"### `{ticker}` — {m['title']}")
        p(f"Volume: {m['vol']:,} | Spread: {m['spread']}c")
        p()

        try:
            trades = get_trades(ticker, limit=100)
        except Exception as e:
            p(f"Error fetching trades: {e}")
            p()
            continue

        if not trades:
            p("No recent trades found.")
            p()
            continue

        p(f"Recent trades fetched: **{len(trades)}**")

        # Parse timestamps and sizes
        timestamps = []
        sizes = []
        for t in trades:
            ts = t.get("created_time", "") or t.get("ts", "")
            count = t.get("count", 0) or t.get("contracts", 0) or 1
            sizes.append(count)

            if ts:
                try:
                    if isinstance(ts, str):
                        ts_parsed = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
                    else:
                        ts_parsed = float(ts)
                    timestamps.append(ts_parsed)
                except (ValueError, TypeError):
                    pass

        if timestamps:
            timestamps.sort(reverse=True)
            time_span = timestamps[0] - timestamps[-1] if len(timestamps) > 1 else 0
            trades_per_hour = (len(timestamps) / (time_span / 3600)) if time_span > 0 else 0

            gaps = [timestamps[i] - timestamps[i+1] for i in range(len(timestamps)-1)]
            median_gap = statistics.median(gaps) if gaps else 0

            p(f"- Time span of {len(trades)} trades: **{time_span/3600:.1f}h**")
            p(f"- Trades per hour: **{trades_per_hour:.1f}**")
            p(f"- Median time between trades: **{median_gap:.0f}s** ({median_gap/60:.1f}m)")
        else:
            p("- Could not parse trade timestamps")

        if sizes:
            p(f"- Median trade size: **{statistics.median(sizes):.0f}** contracts")
            p(f"- Mean trade size: **{statistics.mean(sizes):.1f}** contracts")
            p(f"- Max trade size: **{max(sizes)}** contracts")

        # Show a few sample trades
        p()
        p("Sample trades:")
        p("| Time | Side | Price | Size |")
        p("|---|---|---|---|")
        for t in trades[:10]:
            ts = t.get("created_time", "?")
            if isinstance(ts, str) and len(ts) > 19:
                ts = ts[11:19]  # Just the time part
            side = t.get("taker_side", t.get("side", "?"))
            price = t.get("yes_price", t.get("price", "?"))
            count = t.get("count", t.get("contracts", "?"))
            p(f"| {ts} | {side} | {price}c | {count} |")
        p()

        time.sleep(0.2)

    p("### Comparison with KXBTCD:")
    # Probe KXBTCD ATM for comparison
    try:
        kxbtcd_trades = get_trades("KXBTCD-26FEB2717-T66999.99", limit=100)
        if kxbtcd_trades:
            ts_list = []
            sz_list = []
            for t in kxbtcd_trades:
                ts = t.get("created_time", "")
                count = t.get("count", 1)
                sz_list.append(count)
                if ts:
                    try:
                        ts_list.append(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp())
                    except (ValueError, TypeError):
                        pass
            if len(ts_list) > 1:
                ts_list.sort(reverse=True)
                span = ts_list[0] - ts_list[-1]
                tph = (len(ts_list) / (span / 3600)) if span > 0 else 0
                p(f"- KXBTCD ATM: **{tph:.1f}** trades/hour, median size **{statistics.median(sz_list):.0f}**")
            else:
                p("- KXBTCD ATM: insufficient trade data")
        else:
            p("- KXBTCD ATM: no recent trades found")
    except Exception as e:
        p(f"- KXBTCD comparison error: {e}")
    p()

    # ================================================================
    # SECTION 5: Fee structure
    # ================================================================
    p("## 5. Fee Structure")
    p()
    p("Kalshi fee structure (as of 2026):")
    p("- **Crypto (KXBTCD etc)**: taker=7%, maker_mult=0.0175")
    p("  - Maker fee = `0.0175 * P * (1-P)` per contract")
    p("  - At 50c: maker fee = 0.44c, taker fee = 3.5c")
    p()
    p("Sports fee structure — checking market data for fee fields...")
    p()

    # Check if market data includes fee info
    if by_volume:
        sample = by_volume[0]
        fee_fields = {k: v for k, v in sample.items()
                      if "fee" in k.lower() or "cost" in k.lower() or "commission" in k.lower()}
        if fee_fields:
            p(f"Fee fields found in market data: {fee_fields}")
        else:
            p("No explicit fee fields in market data. Fee info may be:")
            p("- In the /exchange/schedule endpoint")
            p("- Series-level (check series metadata)")
            p("- Same formula as crypto (0.07 taker, 0.0175 maker_mult)")
            p()
            # Check if series data has fee info
            all_fields = set()
            for m in by_volume[:3]:
                all_fields.update(m.keys())
            p(f"Available market fields: `{sorted(f for f in all_fields if not f.startswith('_'))}`")
    p()

    # ================================================================
    # SECTION 6: Market lifecycle
    # ================================================================
    p("## 6. Market Lifecycle")
    p()

    # Analyze event timing
    for ev in sports_events[:5]:
        title = (ev.get("title", "") or "?")[:60]
        open_time = ev.get("open_time", ev.get("created_time", "?"))
        close_time = ev.get("close_time", ev.get("expiration_time", "?"))
        p(f"**{title}**")
        p(f"- Open: {open_time}")
        p(f"- Close: {close_time}")

        # Check markets in this event for lifecycle data
        mkts = ev.get("_markets", [])
        if mkts:
            sample_m = mkts[0]
            game_start = sample_m.get("game_start_time", sample_m.get("event_start_time", "n/a"))
            m_open = sample_m.get("open_time", "n/a")
            m_close = sample_m.get("close_time", sample_m.get("expiration_time", "n/a"))
            status = sample_m.get("status", "?")
            result = sample_m.get("result", "?")
            p(f"- Sample market status: {status}, result: {result}")
            p(f"- Market open: {m_open}")
            p(f"- Market close: {m_close}")
            p(f"- Game start: {game_start}")

            # List all available fields for one market
            if ev == sports_events[0]:
                p(f"- All fields: `{sorted(sample_m.keys())}`")
        p()

    p("### Key lifecycle questions:")
    p("- Sports markets typically open 1-7 days before the game")
    p("- Most volume comes in the last few hours before tip-off")
    p("- In-game (live) markets: check for markets with close_time AFTER game start")
    p("- Game cancellation risk: market may void, positions returned at cost basis")
    p()

    # ================================================================
    # SECTION 7: Verdict
    # ================================================================
    p("## 7. The Verdict")
    p()

    total_sports_vol = sum(m["_vol"] for m in all_sports_markets)
    total_with_spread = len([m for m in top_markets_detail if m.get("spread") is not None])
    avg_spread = (statistics.mean([m["spread"] for m in top_markets_detail
                                   if m.get("spread") is not None])
                  if total_with_spread else 0)

    p(f"### Summary Statistics")
    p(f"- Total sports events found: **{len(sports_events)}**")
    p(f"- Total sports markets: **{len(all_sports_markets)}**")
    p(f"- Multi-contract events (5+): **{len(multi_contract_events)}**")
    p(f"- Total volume across sports: **{total_sports_vol:,}** contracts")
    p(f"- Average spread (top 20): **{avg_spread:.1f}c**")
    p()

    p("### Assessment for Market Making")
    p()
    p("**a) Wide enough spreads?**")
    p(f"- Top 20: {spread_counts['gt3']} with >3c, {spread_counts['gt5']} with >5c, {spread_counts['gt10']} with >10c")
    p()
    p("**b) Trade frequency?**")
    for m in active_markets[:3]:
        p(f"- {m['ticker']}: see Section 4")
    p()
    p("**c) Multi-contract events?**")
    p(f"- {len(multi_contract_events)} events with 5+ contracts")
    p()
    p("**d) Fees?**")
    p("- Likely same parabolic formula as crypto. Needs verification from Kalshi docs.")
    p()
    p("**e) Volume vs KXBTCD?**")
    if by_volume:
        top_vol = by_volume[0]["_vol"]
        p(f"- Top sports market: {top_vol:,} contracts")
    p()
    p("**f) Unique risks?**")
    p("- Game cancellation / postponement")
    p("- Pregame vs in-game volatility")
    p("- Short market lifetime (hours, not days)")
    p("- Volume concentrated in last hours before game")
    p()

    p("### Recommendation")
    p()
    p("*Based on the data above — see specific numbers to draw conclusions.*")
    p("*Key factors: need spreads >5c, trades/hour >10, and depth >20 to MM profitably.*")

    # Write to file
    report = "\n".join(out)
    report_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sports_feasibility.md")
    with open(report_path, "w") as f:
        f.write(report)
    print(f"\n\n--- Report saved to {report_path} ---")


if __name__ == "__main__":
    main()
