"""Market Making Reality Check — Directional Decomposition + Queue-Adjusted Simulation"""
import sys, os, sqlite3, statistics, math
from collections import defaultdict
from datetime import datetime, timezone
sys.path.insert(0, "/opt/kalshi-arb-bot")

DB = "/opt/kalshi-arb-bot/arb_bot.db"
OUT = "/opt/kalshi-arb-bot/mm_reality_check.md"
MAKER_MULT = 0.0175

def maker_fee(price_cents):
    p = price_cents / 100.0
    return MAKER_MULT * p * (1 - p) * 100

def epoch_to_str(ep):
    return datetime.fromtimestamp(ep, tz=timezone.utc).strftime("%m-%d %H:%M")

def get_enriched(conn, ticker, series):
    """Reconstruct order flow: trades merged with ladder snapshots."""
    cur = conn.cursor()
    cur.execute("SELECT timestamp, yes_price, count, taker_side FROM market_trades WHERE ticker=? ORDER BY timestamp", (ticker,))
    trades = [{"ts": r[0], "price": r[1], "count": r[2], "side": r[3]} for r in cur.fetchall()]

    parts = ticker.split("-T")
    strike = float(parts[-1]) if len(parts) >= 2 else 0

    cur.execute("SELECT DISTINCT expiry_window FROM ladder_snapshots WHERE series_ticker=? AND strike=? LIMIT 1", (series, strike))
    ew_row = cur.fetchone()
    expiry_window = ew_row[0] if ew_row else None

    snapshots = []
    if expiry_window:
        cur.execute("""SELECT timestamp, yes_ask, yes_bid, no_ask, no_bid, yes_depth, no_depth
            FROM ladder_snapshots WHERE series_ticker=? AND strike=? AND expiry_window=?
            ORDER BY timestamp""", (series, strike, expiry_window))
        snapshots = [{"ts": r[0], "ya": r[1], "yb": r[2], "na": r[3], "nb": r[4], "yd": r[5], "nd": r[6]} for r in cur.fetchall()]

    snap_idx = 0
    enriched = []
    for t in trades:
        while snap_idx < len(snapshots) - 1 and snapshots[snap_idx + 1]["ts"] <= t["ts"]:
            snap_idx += 1
        book = snapshots[snap_idx] if snap_idx < len(snapshots) else None
        enriched.append({**t, "book": book})

    return enriched, snapshots


def run():
    conn = sqlite3.connect(DB)
    lines = []
    def w(s=""):
        lines.append(s)

    w("# Market Making Reality Check")
    w(f"**Generated**: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    w()

    ticker = "KXBTCD-26FEB2717-T67999.99"
    series = "KXBTCD"
    enriched, snapshots = get_enriched(conn, ticker, series)

    w(f"**Market**: {ticker}")
    w(f"**Trades**: {len(enriched):,} | **Snapshots**: {len(snapshots):,}")
    if enriched:
        span = (enriched[-1]["ts"] - enriched[0]["ts"]) / 3600
        w(f"**Time span**: {span:.1f} hours ({span/24:.1f} days)")
    w()

    # ================================================================
    # 1. DIRECTIONAL DECOMPOSITION
    # ================================================================
    w("## 1. Directional Decomposition")
    w()
    w("Running Strategy A (10c half-spread, 50 max inventory) and tracking every fill.")
    w()

    # Replay the strategy, tracking individual fills as a FIFO queue
    inventory = 0  # positive = long, negative = short
    buy_queue = []   # [(price, count, timestamp, fee)]
    sell_queue = []  # [(price, count, timestamp, fee)]
    completed_rts = []  # [(buy_price, sell_price, qty, hold_time, buy_fee, sell_fee)]
    all_fills = []  # [(ts, side, price, count)]
    max_inv = 50
    half_spread = 10

    last_book = None
    for item in enriched:
        if item.get("book"):
            last_book = item["book"]
        if not last_book:
            continue

        ya = last_book["ya"] or 0
        yb = last_book["yb"] or 0
        mid = (ya + yb) / 2 if ya > 0 and yb > 0 else (ya if ya > 0 else yb)
        if mid <= 0:
            continue

        our_bid = max(1, mid - half_spread)
        our_ask = min(99, mid + half_spread)
        if our_bid >= our_ask:
            continue

        price = item["price"]
        count = item["count"]
        ts = item["ts"]

        # Buy fill
        if price <= our_bid and inventory < max_inv:
            qty = min(count, max_inv - inventory)
            fee = maker_fee(price) * qty
            inventory += qty
            buy_queue.append((price, qty, ts, fee))
            all_fills.append((ts, "BUY", price, qty))

            # Try to match with existing short inventory (sell_queue)
            while sell_queue and qty > 0:
                sp, sq, sts, sf = sell_queue[0]
                matched = min(sq, qty)
                completed_rts.append((price, sp, matched, ts - sts, fee * matched / max(1, qty), sf * matched / max(1, sq)))
                qty -= matched
                if matched >= sq:
                    sell_queue.pop(0)
                else:
                    sell_queue[0] = (sp, sq - matched, sts, sf * (sq - matched) / max(1, sq))

        # Sell fill
        if price >= our_ask and inventory > -max_inv:
            qty = min(count, inventory + max_inv)
            if qty <= 0:
                continue
            fee = maker_fee(price) * qty
            inventory -= qty
            sell_queue.append((price, qty, ts, fee))
            all_fills.append((ts, "SELL", price, qty))

            # Try to match with existing long inventory (buy_queue)
            while buy_queue and qty > 0:
                bp, bq, bts, bf = buy_queue[0]
                matched = min(bq, qty)
                completed_rts.append((bp, price, matched, ts - bts, bf * matched / max(1, bq), fee * matched / max(1, qty)))
                qty -= matched
                if matched >= bq:
                    buy_queue.pop(0)
                else:
                    buy_queue[0] = (bp, bq - matched, bts, bf * (bq - matched) / max(1, bq))

    # Calculate spread P&L from completed round trips
    spread_pnl = 0
    spread_fees = 0
    for bp, sp, qty, hold, bf, sf in completed_rts:
        spread_pnl += (sp - bp) * qty
        spread_fees += bf + sf

    spread_net = spread_pnl - spread_fees
    total_rt_contracts = sum(qty for _, _, qty, _, _, _ in completed_rts)

    # Calculate open inventory mark-to-market
    if enriched:
        last_book_final = None
        for item in reversed(enriched):
            if item.get("book"):
                last_book_final = item["book"]
                break
        final_mid = ((last_book_final["ya"] + last_book_final["yb"]) / 2
                     if last_book_final and last_book_final["ya"] and last_book_final["yb"] else 50)
    else:
        final_mid = 50

    # Open inventory
    open_long_value = sum(p * q for p, q, _, _ in buy_queue)
    open_long_qty = sum(q for _, q, _, _ in buy_queue)
    open_long_fees = sum(f for _, _, _, f in buy_queue)
    open_short_value = sum(p * q for p, q, _, _ in sell_queue)
    open_short_qty = sum(q for _, q, _, _ in sell_queue)
    open_short_fees = sum(f for _, _, _, f in sell_queue)

    net_open_qty = open_long_qty - open_short_qty  # positive = long
    if net_open_qty > 0:
        avg_open_price = open_long_value / max(1, open_long_qty)
        directional_pnl = (final_mid - avg_open_price) * net_open_qty
    elif net_open_qty < 0:
        avg_open_price = open_short_value / max(1, open_short_qty)
        directional_pnl = (avg_open_price - final_mid) * abs(net_open_qty)
    else:
        avg_open_price = 0
        directional_pnl = 0

    open_fees = open_long_fees + open_short_fees
    directional_net = directional_pnl - open_fees

    total_net = spread_net + directional_net

    w("### Fill Log")
    w()
    w(f"Total fills: {len(all_fills)}")
    w(f"Completed round trips: {len(completed_rts)} ({total_rt_contracts} contracts)")
    w(f"Open inventory at end: {net_open_qty} contracts (avg price {avg_open_price:.1f}c, final mid {final_mid:.1f}c)")
    w()

    w("| # | Time | Side | Price | Qty | Running Inv |")
    w("|---|------|------|-------|-----|-------------|")
    running_inv = 0
    for i, (ts, side, price, qty) in enumerate(all_fills):
        if side == "BUY":
            running_inv += qty
        else:
            running_inv -= qty
        w(f"| {i+1} | {epoch_to_str(ts)} | {side} | {price}c | {qty} | {running_inv} |")
    w()

    w("### Round Trip Details")
    w()
    if completed_rts:
        w("| # | Buy Price | Sell Price | Qty | Spread | Gross | Fees | Net | Hold Time |")
        w("|---|-----------|-----------|-----|--------|-------|------|-----|-----------|")
        for i, (bp, sp, qty, hold, bf, sf) in enumerate(completed_rts):
            gross = (sp - bp) * qty
            fees = bf + sf
            net = gross - fees
            w(f"| {i+1} | {bp}c | {sp}c | {qty} | {sp-bp}c | {gross:.1f}c | {fees:.1f}c | {net:.1f}c | {hold/60:.0f}m |")
        w()

    w("### P&L Decomposition")
    w()
    w("| Component | Gross | Fees | Net |")
    w("|-----------|-------|------|-----|")
    w(f"| **Spread P&L** (round trips) | {spread_pnl:.1f}c | {spread_fees:.1f}c | **{spread_net:.1f}c (${spread_net/100:.2f})** |")
    w(f"| **Directional P&L** (open inventory) | {directional_pnl:.1f}c | {open_fees:.1f}c | **{directional_net:.1f}c (${directional_net/100:.2f})** |")
    w(f"| **TOTAL** | | | **{total_net:.1f}c (${total_net/100:.2f})** |")
    w()
    w(f"**Spread P&L share**: {100*abs(spread_net)/max(1,abs(spread_net)+abs(directional_net)):.0f}%")
    w(f"**Directional P&L share**: {100*abs(directional_net)/max(1,abs(spread_net)+abs(directional_net)):.0f}%")
    w()

    # Unlucky scenario: what if BTC moved the opposite way?
    if net_open_qty != 0:
        # The directional profit came from price moving from avg_open_price toward final_mid.
        # Opposite scenario: price moved away by the same delta
        delta = final_mid - avg_open_price
        unlucky_final = avg_open_price - delta  # mirror
        if net_open_qty > 0:
            unlucky_dir_pnl = (unlucky_final - avg_open_price) * net_open_qty
        else:
            unlucky_dir_pnl = (avg_open_price - unlucky_final) * abs(net_open_qty)
        unlucky_dir_net = unlucky_dir_pnl - open_fees
        unlucky_total = spread_net + unlucky_dir_net

        w("### Unlucky Scenario (BTC moves opposite direction)")
        w()
        w(f"If price had moved to {unlucky_final:.1f}c instead of {final_mid:.1f}c:")
        w(f"- Directional P&L: **{unlucky_dir_net:.1f}c (${unlucky_dir_net/100:.2f})**")
        w(f"- Total P&L: **{unlucky_total:.1f}c (${unlucky_total/100:.2f})**")
        w(f"- vs actual total: {total_net:.1f}c (${total_net/100:.2f})")
        w()
        if unlucky_total < 0:
            w(f"**In the unlucky scenario, we LOSE ${abs(unlucky_total)/100:.2f}.** "
              f"The spread P&L of ${spread_net/100:.2f} is not enough to cover the directional loss.")
        else:
            w(f"Even in the unlucky scenario, total P&L is positive thanks to spread capture.")
    w()

    # Avg hold time for round trips
    if completed_rts:
        hold_times = [h/60 for _, _, _, h, _, _ in completed_rts]
        w(f"**Round trip hold times**: min={min(hold_times):.0f}m, median={statistics.median(hold_times):.0f}m, max={max(hold_times):.0f}m")
        w()

    # ================================================================
    # 2. QUEUE-ADJUSTED SIMULATION
    # ================================================================
    w("## 2. Queue-Adjusted Simulation")
    w()
    w("Modeling queue priority: we only get filled after all existing depth at our price is consumed.")
    w()

    # For each snapshot, we know the depth at various price levels.
    # Problem: snapshots only give us depth at BEST bid/ask, not at arbitrary levels.
    # So we estimate: if our bid is below best_bid, there's 0 depth ahead (we're alone).
    # If our bid equals best_bid, we're behind yes_depth contracts.
    # If our bid is above best_bid (improving), we're the new best — 0 ahead.
    # Similarly for asks.

    # Strategy: 10c half-spread from mid, max_inv=50
    # Track queue position on each side

    bid_queue_pos = None  # (our_price, contracts_ahead)
    ask_queue_pos = None
    bid_price_current = None
    ask_price_current = None
    inventory_q = 0
    buy_fills_q = []
    sell_fills_q = []
    buy_queue_q = []  # FIFO for round trip matching
    sell_queue_q = []
    completed_rts_q = []
    last_book_q = None

    # Also track: how much total volume traded at each of our price levels
    total_volume_at_bid = 0
    total_volume_at_ask = 0

    for item in enriched:
        if item.get("book"):
            new_book = item["book"]
            ya = new_book["ya"] or 0
            yb = new_book["yb"] or 0
            yd = new_book["yd"] or 0
            nd = new_book["nd"] or 0
            mid = (ya + yb) / 2 if ya > 0 and yb > 0 else (ya if ya > 0 else yb)
            if mid <= 0:
                last_book_q = new_book
                continue

            new_bid = max(1, int(mid - half_spread))
            new_ask = min(99, int(mid + half_spread))

            # If our bid price changed, go to back of new queue
            if new_bid != bid_price_current:
                bid_price_current = new_bid
                # Depth ahead of us at our bid price
                if new_bid > yb:
                    # We're improving — no one ahead
                    bid_queue_pos = 0
                elif new_bid == yb:
                    # We're at best bid — behind existing depth
                    bid_queue_pos = yd
                else:
                    # We're below best bid — only fills if book gets eaten through
                    # Approximate: full depth at best bid + some at our level
                    bid_queue_pos = yd + 500  # conservative estimate
            # Same for ask
            if new_ask != ask_price_current:
                ask_price_current = new_ask
                if new_ask < ya:
                    ask_queue_pos = 0
                elif new_ask == ya:
                    ask_queue_pos = nd
                else:
                    ask_queue_pos = nd + 500

            last_book_q = new_book

        if not last_book_q or bid_price_current is None:
            continue

        price = item["price"]
        count = item["count"]
        ts = item["ts"]

        # Buy side: trades at our bid price or below reduce our queue
        if bid_queue_pos is not None and price <= bid_price_current:
            if price < bid_price_current:
                # Trade below our price — aggressive seller, clears everything at and above
                bid_queue_pos = 0
            total_volume_at_bid += count

            if bid_queue_pos <= 0 and inventory_q < max_inv:
                # We get filled!
                qty = min(count, max_inv - inventory_q)
                fee = maker_fee(bid_price_current) * qty
                inventory_q += qty
                buy_fills_q.append((ts, bid_price_current, qty, fee))
                buy_queue_q.append((bid_price_current, qty, ts, fee))

                # Match with sells
                rem = qty
                while sell_queue_q and rem > 0:
                    sp, sq, sts, sf = sell_queue_q[0]
                    matched = min(sq, rem)
                    completed_rts_q.append((bid_price_current, sp, matched, ts - sts,
                                            fee * matched / max(1, qty), sf * matched / max(1, sq)))
                    rem -= matched
                    if matched >= sq:
                        sell_queue_q.pop(0)
                    else:
                        sell_queue_q[0] = (sp, sq - matched, sts, sf * (sq - matched) / max(1, sq))

                # Reset queue — we just got filled, go to back
                bid_queue_pos = 100  # rough reset
            else:
                bid_queue_pos = max(0, bid_queue_pos - count)

        # Sell side: trades at our ask price or above
        if ask_queue_pos is not None and price >= ask_price_current:
            if price > ask_price_current:
                ask_queue_pos = 0
            total_volume_at_ask += count

            if ask_queue_pos <= 0 and inventory_q > -max_inv:
                qty = min(count, inventory_q + max_inv)
                if qty <= 0:
                    continue
                fee = maker_fee(ask_price_current) * qty
                inventory_q -= qty
                sell_fills_q.append((ts, ask_price_current, qty, fee))
                sell_queue_q.append((ask_price_current, qty, ts, fee))

                rem = qty
                while buy_queue_q and rem > 0:
                    bp, bq, bts, bf = buy_queue_q[0]
                    matched = min(bq, rem)
                    completed_rts_q.append((bp, ask_price_current, matched, ts - bts,
                                            bf * matched / max(1, bq), fee * matched / max(1, qty)))
                    rem -= matched
                    if matched >= bq:
                        buy_queue_q.pop(0)
                    else:
                        buy_queue_q[0] = (bp, bq - matched, bts, bf * (bq - matched) / max(1, bq))

                ask_queue_pos = 100
            else:
                ask_queue_pos = max(0, ask_queue_pos - count)

    # Results
    w("### Queue-Adjusted Fill Log")
    w()
    all_q_fills = [(ts, "BUY", p, q) for ts, p, q, _ in buy_fills_q] + \
                  [(ts, "SELL", p, q) for ts, p, q, _ in sell_fills_q]
    all_q_fills.sort(key=lambda x: x[0])

    w(f"**Buy fills**: {len(buy_fills_q)} ({sum(q for _,_,q,_ in buy_fills_q)} contracts)")
    w(f"**Sell fills**: {len(sell_fills_q)} ({sum(q for _,_,q,_ in sell_fills_q)} contracts)")
    w(f"**Completed round trips**: {len(completed_rts_q)}")
    w(f"**Final inventory**: {inventory_q}")
    w()

    w(f"Total volume that traded at our bid levels: {total_volume_at_bid:,} contracts")
    w(f"Total volume that traded at our ask levels: {total_volume_at_ask:,} contracts")
    w()

    if all_q_fills:
        w("| # | Time | Side | Price | Qty | Running Inv |")
        w("|---|------|------|-------|-----|-------------|")
        running = 0
        for i, (ts, side, price, qty) in enumerate(all_q_fills):
            running += qty if side == "BUY" else -qty
            w(f"| {i+1} | {epoch_to_str(ts)} | {side} | {price}c | {qty} | {running} |")
        w()

    # P&L decomposition for queue-adjusted
    q_spread_pnl = 0
    q_spread_fees = 0
    for bp, sp, qty, hold, bf, sf in completed_rts_q:
        q_spread_pnl += (sp - bp) * qty
        q_spread_fees += bf + sf
    q_spread_net = q_spread_pnl - q_spread_fees

    # Open inventory
    q_open_long_qty = sum(q for _, _, q, _ in buy_queue_q)
    q_open_long_val = sum(p * q for p, q, _, _ in buy_queue_q)
    q_open_long_fee = sum(f for _, _, _, f in buy_queue_q)
    q_open_short_qty = sum(q for _, _, q, _ in sell_queue_q)
    q_open_short_val = sum(p * q for p, q, _, _ in sell_queue_q)
    q_open_short_fee = sum(f for _, _, _, f in sell_queue_q)
    q_net_open = q_open_long_qty - q_open_short_qty

    if q_net_open > 0:
        q_avg_open = q_open_long_val / max(1, q_open_long_qty)
        q_dir_pnl = (final_mid - q_avg_open) * q_net_open
    elif q_net_open < 0:
        q_avg_open = q_open_short_val / max(1, q_open_short_qty)
        q_dir_pnl = (q_avg_open - final_mid) * abs(q_net_open)
    else:
        q_avg_open = 0
        q_dir_pnl = 0
    q_open_fees = q_open_long_fee + q_open_short_fee
    q_dir_net = q_dir_pnl - q_open_fees
    q_total = q_spread_net + q_dir_net

    w("### Queue-Adjusted P&L")
    w()
    w("| Component | Gross | Fees | Net |")
    w("|-----------|-------|------|-----|")
    w(f"| Spread P&L | {q_spread_pnl:.1f}c | {q_spread_fees:.1f}c | **{q_spread_net:.1f}c (${q_spread_net/100:.2f})** |")
    w(f"| Directional P&L | {q_dir_pnl:.1f}c | {q_open_fees:.1f}c | **{q_dir_net:.1f}c (${q_dir_net/100:.2f})** |")
    w(f"| **TOTAL** | | | **{q_total:.1f}c (${q_total/100:.2f})** |")
    w()

    # ================================================================
    # COMPARISON
    # ================================================================
    w("## 3. Comparison: Naive vs Queue-Adjusted vs Reality")
    w()
    w("| Metric | Naive Sim | Queue-Adjusted |")
    w("|--------|-----------|----------------|")
    w(f"| Buy fills | {len(all_fills)//2} | {len(buy_fills_q)} |")
    w(f"| Sell fills | {len(all_fills)//2} | {len(sell_fills_q)} |")
    w(f"| Completed RTs | {len(completed_rts)} | {len(completed_rts_q)} |")
    w(f"| Spread P&L | ${spread_net/100:.2f} | ${q_spread_net/100:.2f} |")
    w(f"| Directional P&L | ${directional_net/100:.2f} | ${q_dir_net/100:.2f} |")
    w(f"| Total P&L | ${total_net/100:.2f} | ${q_total/100:.2f} |")
    w(f"| Final inventory | {net_open_qty} | {inventory_q} |")
    w()

    # ================================================================
    # VERDICT
    # ================================================================
    w("## 4. Verdict")
    w()

    total_q_fills = len(buy_fills_q) + len(sell_fills_q)
    w(f"**Queue-adjusted fills over {span:.0f} hours**: {total_q_fills}")
    w()

    if total_q_fills < 5:
        w("### **PASSIVE MARKET MAKING IS NOT VIABLE.**")
        w()
        w(f"With queue modeling, we get **{total_q_fills} fills in {span/24:.1f} days**.")
        w("The existing depth at each price level means our resting orders almost never")
        w("reach the front of the queue before the price moves and we have to requote.")
        w()
        w("**Options:**")
        w("1. **Aggressive crossing** — take liquidity instead of providing it (pay taker fees, need edge)")
        w("2. **Quote at prices with no existing depth** — wider spreads but guaranteed queue priority")
        w("3. **Focus on illiquid strikes** — less competition but less flow")
        w("4. **Abandon MM entirely** — the market structure doesn't support passive strategies")
    elif total_q_fills < 20:
        w("### **MARGINAL — very low fill rate.**")
        w()
        w(f"Queue-adjusted simulation produces {total_q_fills} fills over {span/24:.1f} days.")
        w(f"Spread P&L: ${q_spread_net/100:.2f} | Directional P&L: ${q_dir_net/100:.2f}")
        if abs(q_dir_net) > abs(q_spread_net) * 2:
            w("**Most of the P&L is directional, not from spread capture.**")
            w("This is BTC price speculation disguised as market making.")
    else:
        if q_spread_net > 0:
            w("### **SPREAD CAPTURE IS REAL**")
            w(f"Queue-adjusted spread P&L: ${q_spread_net/100:.2f} over {span/24:.1f} days")
        else:
            w("### **SPREAD P&L IS NEGATIVE** — adverse selection dominates.")

    w()

    # Final assessment of the naive sim's $95
    naive_total_approx = total_net
    w("### Decomposition of the Naive Sim's \"$95 profit\"")
    w()
    w(f"- Spread capture (real MM profit): **${spread_net/100:.2f}** ({100*abs(spread_net)/max(1,abs(naive_total_approx)):.0f}%)")
    w(f"- Directional exposure (BTC bet): **${directional_net/100:.2f}** ({100*abs(directional_net)/max(1,abs(naive_total_approx)):.0f}%)")
    w()
    if abs(directional_net) > abs(spread_net):
        pct = 100 * abs(directional_net) / max(1, abs(naive_total_approx))
        w(f"**{pct:.0f}% of the profit was from directional BTC exposure, not market making.**")
        w("The simulation was profitable because BTC price moved favorably while we held inventory.")
        w("A market maker should be profitable regardless of price direction.")

    w()
    w("---")
    w(f"*Report generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}*")

    report = "\n".join(lines)
    with open(OUT, "w") as f:
        f.write(report)
    print(f"Report written to {OUT} ({len(lines)} lines)")

if __name__ == "__main__":
    run()
