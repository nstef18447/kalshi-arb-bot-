"""Phase 1 Restart Analysis — Above/Below Contracts Only"""
import sqlite3
import json
from datetime import datetime, timezone
from collections import defaultdict

DB = "/opt/kalshi-arb-bot/arb_bot.db"
OUT = "/opt/kalshi-arb-bot/phase1_ab_report.md"
AB_SERIES = ("KXBTCD", "KXETHD", "KXSOLD")

def q(cur, sql, params=()):
    cur.execute(sql, params)
    cols = [d[0] for d in cur.description] if cur.description else []
    return [dict(zip(cols, r)) for r in cur.fetchall()]

def fmt_ts(ts):
    if ts is None:
        return "N/A"
    try:
        return datetime.fromisoformat(ts).strftime("%Y-%m-%d %H:%M:%S")
    except:
        return str(ts)[:19]

def run():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    lines = []
    def w(s=""):
        lines.append(s)

    w("# Phase 1 Restart Analysis — Above/Below Contracts")
    w(f"**Generated**: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    w(f"**Series**: KXBTCD, KXETHD, KXSOLD (above/below only)")
    w(f"**Database**: {DB}")
    w()

    # =========================================================
    # 1. DATA SUMMARY
    # =========================================================
    w("## 1. Data Summary")
    w()

    for s in AB_SERIES:
        rows = q(cur, """
            SELECT COUNT(*) as cnt,
                   MIN(timestamp) as first_ts,
                   MAX(timestamp) as last_ts
            FROM ladder_snapshots WHERE series_ticker = ?
        """, (s,))
        r = rows[0]
        first = r["first_ts"]
        last = r["last_ts"]
        if first and last:
            try:
                t0 = datetime.fromisoformat(first)
                t1 = datetime.fromisoformat(last)
                hours = (t1 - t0).total_seconds() / 3600
            except:
                hours = 0
        else:
            hours = 0
        w(f"**{s}**: {r['cnt']:,} ladder rows | {hours:.1f} hours | {fmt_ts(first)} → {fmt_ts(last)}")

    # Scan count
    w()
    for s in AB_SERIES:
        rows = q(cur, "SELECT COUNT(*) as cnt FROM scans WHERE series_ticker = ?", (s,))
        w(f"**{s}** scan cycles: {rows[0]['cnt']:,}")

    # Scan gaps
    w()
    w("### Scan Gaps > 60 seconds")
    for s in AB_SERIES:
        rows = q(cur, """
            SELECT timestamp FROM scans
            WHERE series_ticker = ?
            ORDER BY timestamp
        """, (s,))
        gaps = []
        for i in range(1, len(rows)):
            try:
                t0 = datetime.fromisoformat(rows[i-1]["timestamp"])
                t1 = datetime.fromisoformat(rows[i]["timestamp"])
                gap = (t1 - t0).total_seconds()
                if gap > 60:
                    gaps.append((rows[i-1]["timestamp"], rows[i]["timestamp"], gap))
            except:
                pass
        if gaps:
            w(f"**{s}**: {len(gaps)} gaps > 60s")
            for g in gaps[:5]:
                w(f"  - {fmt_ts(g[0])} → {fmt_ts(g[1])}: {g[2]:.0f}s")
            if len(gaps) > 5:
                w(f"  - ... and {len(gaps)-5} more")
        else:
            w(f"**{s}**: No gaps > 60s")
    w()

    # =========================================================
    # 2. SANITY CHECK
    # =========================================================
    w("## 2. Sanity Check — Ladder Structure")
    w()

    for s in AB_SERIES:
        w(f"### {s} — Sample Ladder (10 rows, most recent scan)")
        # Get most recent scan's expiry window
        rows = q(cur, """
            SELECT timestamp, expiry_window FROM ladder_snapshots
            WHERE series_ticker = ?
            ORDER BY timestamp DESC LIMIT 1
        """, (s,))
        if not rows:
            w("No data.")
            w()
            continue
        ts = rows[0]["timestamp"]
        ew = rows[0]["expiry_window"]
        sample = q(cur, """
            SELECT strike, yes_ask, no_ask, yes_ask + no_ask as combined
            FROM ladder_snapshots
            WHERE series_ticker = ? AND timestamp = ? AND expiry_window = ?
              AND yes_ask > 0 AND no_ask > 0
            ORDER BY strike
            LIMIT 10
        """, (s, ts, ew))
        if sample:
            w(f"Scan: {fmt_ts(ts)} | Expiry: {ew}")
            w("| Strike | yes_ask | no_ask | Combined |")
            w("|--------|---------|--------|----------|")
            for r in sample:
                w(f"| {r['strike']} | {r['yes_ask']} | {r['no_ask']} | {r['combined']} |")
        w()

        # Monotonicity check
        rows = q(cur, """
            SELECT timestamp, expiry_window, strike, yes_ask
            FROM ladder_snapshots
            WHERE series_ticker = ? AND yes_ask > 0
            ORDER BY timestamp, expiry_window, strike
        """, (s,))
        violations = 0
        total_ladders = 0
        prev_key = None
        prev_ask = None
        for r in rows:
            key = (r["timestamp"], r["expiry_window"])
            if key != prev_key:
                prev_key = key
                prev_ask = r["yes_ask"]
                total_ladders += 1
                continue
            if r["yes_ask"] > prev_ask:
                violations += 1
            prev_ask = r["yes_ask"]
        w(f"**{s} monotonicity**: {violations:,} violations across {total_ladders:,} ladder scans")

        # Combined > 130
        rows = q(cur, """
            SELECT COUNT(*) as cnt FROM ladder_snapshots
            WHERE series_ticker = ? AND yes_ask > 0 AND no_ask > 0
              AND (yes_ask + no_ask) >= 130
        """, (s,))
        total = q(cur, """
            SELECT COUNT(*) as cnt FROM ladder_snapshots
            WHERE series_ticker = ? AND yes_ask > 0 AND no_ask > 0
        """, (s,))
        w(f"**{s} combined >= 130**: {rows[0]['cnt']:,} / {total[0]['cnt']:,} rows ({100*rows[0]['cnt']/max(1,total[0]['cnt']):.1f}%)")
        w()

    # Per-strike combined distribution
    w("### Combined Cost Distribution (all series)")
    for s in AB_SERIES:
        rows = q(cur, """
            SELECT
                SUM(CASE WHEN yes_ask+no_ask < 100 THEN 1 ELSE 0 END) as below_100,
                SUM(CASE WHEN yes_ask+no_ask >= 100 AND yes_ask+no_ask < 105 THEN 1 ELSE 0 END) as r100_105,
                SUM(CASE WHEN yes_ask+no_ask >= 105 AND yes_ask+no_ask < 110 THEN 1 ELSE 0 END) as r105_110,
                SUM(CASE WHEN yes_ask+no_ask >= 110 AND yes_ask+no_ask < 120 THEN 1 ELSE 0 END) as r110_120,
                SUM(CASE WHEN yes_ask+no_ask >= 120 AND yes_ask+no_ask < 130 THEN 1 ELSE 0 END) as r120_130,
                SUM(CASE WHEN yes_ask+no_ask >= 130 THEN 1 ELSE 0 END) as r130_plus,
                COUNT(*) as total
            FROM ladder_snapshots
            WHERE series_ticker = ? AND yes_ask > 0 AND no_ask > 0
        """, (s,))
        r = rows[0]
        t = max(1, r["total"])
        w(f"**{s}** (n={t:,}):")
        w(f"  - < 100: {r['below_100']:,} ({100*r['below_100']/t:.2f}%)")
        w(f"  - 100-104: {r['r100_105']:,} ({100*r['r100_105']/t:.2f}%)")
        w(f"  - 105-109: {r['r105_110']:,} ({100*r['r105_110']/t:.2f}%)")
        w(f"  - 110-119: {r['r110_120']:,} ({100*r['r110_120']/t:.2f}%)")
        w(f"  - 120-129: {r['r120_130']:,} ({100*r['r120_130']/t:.2f}%)")
        w(f"  - 130+: {r['r130_plus']:,} ({100*r['r130_plus']/t:.2f}%)")
    w()

    # =========================================================
    # 3. HARD ARB DETECTION
    # =========================================================
    w("## 3. Hard Arb Detection")
    w()

    total_hard = 0
    for s in AB_SERIES:
        rows = q(cur, """
            SELECT COUNT(*) as cnt FROM opportunities
            WHERE series_ticker = ? AND sub_type = 'hard'
        """, (s,))
        cnt = rows[0]["cnt"]
        total_hard += cnt
        w(f"**{s}**: {cnt} hard arbs detected")

    w()
    if total_hard == 0:
        w("### **ZERO hard arbs detected across all above/below series.**")
        w("This is the key Phase 1 finding for above/below contracts.")
    else:
        w(f"### Total hard arbs: {total_hard}")
        # Deduplicate and show details
        for s in AB_SERIES:
            rows = q(cur, """
                SELECT DISTINCT strike_low, strike_high, expiry_window,
                       MIN(combined_cost) as min_cost, MAX(combined_cost) as max_cost,
                       AVG(depth_thin_side) as avg_depth, COUNT(*) as appearances,
                       MIN(timestamp) as first_seen, MAX(timestamp) as last_seen,
                       AVG(estimated_profit) as avg_profit,
                       AVG(estimated_profit_maker) as avg_profit_maker
                FROM opportunities
                WHERE series_ticker = ? AND sub_type = 'hard'
                GROUP BY strike_low, strike_high, expiry_window
                ORDER BY min_cost
                LIMIT 20
            """, (s,))
            if rows:
                w(f"\n**{s} Hard Arbs** (deduplicated):")
                w("| Strikes | Expiry | Min Cost | Max Cost | Avg Depth | Appearances | Avg Profit (taker) | Avg Profit (maker) |")
                w("|---------|--------|----------|----------|-----------|-------------|--------------------|--------------------|")
                for r in rows:
                    w(f"| {r['strike_low']}-{r['strike_high']} | {r['expiry_window'][:16] if r['expiry_window'] else 'N/A'} | {r['min_cost']} | {r['max_cost']} | {r['avg_depth']:.0f} | {r['appearances']} | {r['avg_profit']:.1f}c | {r['avg_profit_maker']:.1f}c |")
    w()

    # Also check all opp types
    w("### All Opportunity Types Detected")
    for s in AB_SERIES:
        rows = q(cur, """
            SELECT opp_type, sub_type, COUNT(*) as cnt
            FROM opportunities WHERE series_ticker = ?
            GROUP BY opp_type, sub_type ORDER BY cnt DESC
        """, (s,))
        if rows:
            w(f"**{s}**:")
            for r in rows:
                w(f"  - {r['opp_type']}/{r['sub_type']}: {r['cnt']:,}")
        else:
            w(f"**{s}**: No opportunities of any type")
    w()

    # =========================================================
    # 4. NEAR-MISS ANALYSIS
    # =========================================================
    w("## 4. Near-Miss Analysis")
    w()
    w("For each scan cycle, find the cheapest cross-strike pair across all valid adjacent pairs.")
    w()

    # We need to compute cross-strike combined costs from ladder_snapshots
    # For each scan (timestamp, expiry_window, series), build ladder ordered by strike
    # For each pair of strikes (low, high) where low < high:
    #   cross_cost = yes_ask(low) + no_ask(high)
    # Find min per scan

    for s in AB_SERIES:
        w(f"### {s} — Cross-Strike Near Misses")

        # Get all ladder data grouped by scan
        rows = q(cur, """
            SELECT timestamp, expiry_window, strike, yes_ask, no_ask, yes_depth, no_depth
            FROM ladder_snapshots
            WHERE series_ticker = ? AND yes_ask > 0 AND no_ask > 0
            ORDER BY timestamp, expiry_window, strike
        """, (s,))

        if not rows:
            w("No data.")
            w()
            continue

        # Group by (timestamp, expiry_window)
        scans = defaultdict(list)
        for r in rows:
            scans[(r["timestamp"], r["expiry_window"])].append(r)

        scan_mins = []  # (min_combined, ts, ew, strike_low, strike_high, yes_ask_low, no_ask_high, depth_low, depth_high)
        for (ts, ew), strikes in scans.items():
            if len(strikes) < 2:
                continue
            best = None
            for i in range(len(strikes)):
                for j in range(i+1, len(strikes)):
                    low = strikes[i]
                    high = strikes[j]
                    cross = low["yes_ask"] + high["no_ask"]
                    if best is None or cross < best[0]:
                        best = (cross, ts, ew, low["strike"], high["strike"],
                                low["yes_ask"], high["no_ask"],
                                low.get("yes_depth", 0), high.get("no_depth", 0))
            if best:
                scan_mins.append(best)

        if not scan_mins:
            w("No valid cross-strike pairs found.")
            w()
            continue

        scan_mins.sort(key=lambda x: x[0])
        costs = [x[0] for x in scan_mins]

        # Distribution
        below_100 = sum(1 for c in costs if c < 100)
        below_101 = sum(1 for c in costs if c < 101)
        below_102 = sum(1 for c in costs if c < 102)
        below_105 = sum(1 for c in costs if c < 105)

        n = len(costs)
        median = costs[n//2]
        p10 = costs[n//10]
        p25 = costs[n//4]
        p75 = costs[3*n//4]
        p90 = costs[9*n//10]
        minimum = costs[0]

        w(f"**{len(scan_mins):,}** scan-expiry windows analyzed")
        w(f"- Minimum combined cost ever seen: **{minimum}**")
        w(f"- Median min combined: **{median}**")
        w(f"- P10/P25/P75/P90: {p10} / {p25} / {p75} / {p90}")
        w(f"- Below 100 (hard arb): **{below_100}** ({100*below_100/n:.2f}%)")
        w(f"- Below 101: **{below_101}** ({100*below_101/n:.2f}%)")
        w(f"- Below 102: **{below_102}** ({100*below_102/n:.2f}%)")
        w(f"- Below 105: **{below_105}** ({100*below_105/n:.2f}%)")
        w()

        # Top 20 closest
        w(f"**Top 20 closest-to-arb observations:**")
        w("| # | Combined | Timestamp | Expiry | Strike Low | Strike High | yes_ask(low) | no_ask(high) | Depth(low) | Depth(high) |")
        w("|---|----------|-----------|--------|------------|-------------|--------------|--------------|------------|-------------|")
        for i, obs in enumerate(scan_mins[:20]):
            w(f"| {i+1} | {obs[0]} | {fmt_ts(obs[1])} | {str(obs[2])[:16] if obs[2] else 'N/A'} | {obs[3]} | {obs[4]} | {obs[5]} | {obs[6]} | {obs[7]} | {obs[8]} |")
        w()

    # =========================================================
    # 5. SPREAD DYNAMICS BY TIME
    # =========================================================
    w("## 5. Spread Dynamics by Time")
    w()

    for s in AB_SERIES:
        w(f"### {s} — Min Combined Cost by Hour of Day (UTC)")

        rows = q(cur, """
            SELECT timestamp, expiry_window, strike, yes_ask, no_ask
            FROM ladder_snapshots
            WHERE series_ticker = ? AND yes_ask > 0 AND no_ask > 0
            ORDER BY timestamp, expiry_window, strike
        """, (s,))

        scans = defaultdict(list)
        for r in rows:
            scans[(r["timestamp"], r["expiry_window"])].append(r)

        hourly_mins = defaultdict(list)  # hour -> [min_costs]
        expiry_mins = defaultdict(list)  # "near"/"far" -> [min_costs]

        for (ts, ew), strikes in scans.items():
            if len(strikes) < 2:
                continue
            best_cost = None
            for i in range(len(strikes)):
                for j in range(i+1, len(strikes)):
                    cross = strikes[i]["yes_ask"] + strikes[j]["no_ask"]
                    if best_cost is None or cross < best_cost:
                        best_cost = cross
            if best_cost is None:
                continue

            try:
                hour = datetime.fromisoformat(ts).hour
            except:
                continue
            hourly_mins[hour].append(best_cost)

            # Near vs far expiry
            try:
                ts_dt = datetime.fromisoformat(ts)
                ew_dt = datetime.fromisoformat(ew) if ew else None
                if ew_dt:
                    tte = (ew_dt - ts_dt).total_seconds() / 3600
                    if tte < 2:
                        expiry_mins["<2h"].append(best_cost)
                    elif tte < 12:
                        expiry_mins["2-12h"].append(best_cost)
                    else:
                        expiry_mins[">12h"].append(best_cost)
            except:
                pass

        if hourly_mins:
            w("| Hour | Scans | Median Min | P10 | Min |")
            w("|------|-------|------------|-----|-----|")
            for h in range(24):
                if h in hourly_mins:
                    vals = sorted(hourly_mins[h])
                    n = len(vals)
                    med = vals[n//2]
                    p10 = vals[n//10] if n >= 10 else vals[0]
                    w(f"| {h:02d} | {n} | {med} | {p10} | {vals[0]} |")
            w()

            # Text histogram
            w("**Median min combined by hour (text chart):**")
            w("```")
            for h in range(24):
                if h in hourly_mins:
                    vals = sorted(hourly_mins[h])
                    med = vals[len(vals)//2]
                    bar_len = max(0, med - 95)
                    w(f"{h:02d}:00 | {'█' * bar_len} {med}")
            w("```")
            w()

        if expiry_mins:
            w("**By time-to-expiry:**")
            w("| Window | Scans | Median Min | P10 | Min |")
            w("|--------|-------|------------|-----|-----|")
            for label in ["<2h", "2-12h", ">12h"]:
                if label in expiry_mins:
                    vals = sorted(expiry_mins[label])
                    n = len(vals)
                    med = vals[n//2]
                    p10 = vals[n//10] if n >= 10 else vals[0]
                    w(f"| {label} | {n} | {med} | {p10} | {vals[0]} |")
            w()
        w()

    # =========================================================
    # 6. PER-STRIKE SPREAD ANALYSIS
    # =========================================================
    w("## 6. Per-Strike Spread Analysis")
    w()

    for s in AB_SERIES:
        w(f"### {s}")

        # Widest spreads
        rows = q(cur, """
            SELECT strike,
                   AVG(yes_ask + no_ask) as avg_combined,
                   MIN(yes_ask + no_ask) as min_combined,
                   COUNT(*) as obs
            FROM ladder_snapshots
            WHERE series_ticker = ? AND yes_ask > 0 AND no_ask > 0
            GROUP BY strike
            ORDER BY avg_combined DESC
            LIMIT 10
        """, (s,))
        if rows:
            w("**Widest average spreads (top 10 strikes):**")
            w("| Strike | Avg Combined | Min Combined | Observations |")
            w("|--------|-------------|-------------|-------------|")
            for r in rows:
                w(f"| {r['strike']} | {r['avg_combined']:.1f} | {r['min_combined']} | {r['obs']:,} |")
            w()

        # Tightest spreads
        rows = q(cur, """
            SELECT strike,
                   AVG(yes_ask + no_ask) as avg_combined,
                   MIN(yes_ask + no_ask) as min_combined,
                   COUNT(*) as obs
            FROM ladder_snapshots
            WHERE series_ticker = ? AND yes_ask > 0 AND no_ask > 0
            GROUP BY strike
            ORDER BY avg_combined ASC
            LIMIT 10
        """, (s,))
        if rows:
            w("**Tightest average spreads (top 10 strikes):**")
            w("| Strike | Avg Combined | Min Combined | Observations |")
            w("|--------|-------------|-------------|-------------|")
            for r in rows:
                w(f"| {r['strike']} | {r['avg_combined']:.1f} | {r['min_combined']} | {r['obs']:,} |")
            w()

        # Single-strike arbs (combined < 100)
        rows = q(cur, """
            SELECT COUNT(*) as cnt FROM ladder_snapshots
            WHERE series_ticker = ? AND yes_ask > 0 AND no_ask > 0
              AND (yes_ask + no_ask) < 100
        """, (s,))
        cnt = rows[0]["cnt"]
        w(f"**Single-strike arbs (yes_ask + no_ask < 100):** {cnt}")
        if cnt > 0:
            examples = q(cur, """
                SELECT timestamp, strike, yes_ask, no_ask, yes_ask+no_ask as combined
                FROM ladder_snapshots
                WHERE series_ticker = ? AND yes_ask > 0 AND no_ask > 0
                  AND (yes_ask + no_ask) < 100
                ORDER BY yes_ask + no_ask
                LIMIT 10
            """, (s,))
            w("| Timestamp | Strike | yes_ask | no_ask | Combined |")
            w("|-----------|--------|---------|--------|----------|")
            for r in examples:
                w(f"| {fmt_ts(r['timestamp'])} | {r['strike']} | {r['yes_ask']} | {r['no_ask']} | {r['combined']} |")
        w()

    # =========================================================
    # 7. COMPARISON TO RANGE CONTRACTS
    # =========================================================
    w("## 7. Comparison to Range Contracts")
    w()

    comparisons = [("KXBTCD", "KXBTC"), ("KXETHD", "KXETH"), ("KXSOLD", "KXSOLE")]
    for ab, rng in comparisons:
        ab_rows = q(cur, """
            SELECT AVG(yes_ask + no_ask) as avg_combined
            FROM ladder_snapshots
            WHERE series_ticker = ? AND yes_ask > 0 AND no_ask > 0
        """, (ab,))
        rng_rows = q(cur, """
            SELECT AVG(yes_ask + no_ask) as avg_combined
            FROM ladder_snapshots
            WHERE series_ticker = ? AND yes_ask > 0 AND no_ask > 0
        """, (rng,))

        ab_avg = ab_rows[0]["avg_combined"] if ab_rows and ab_rows[0]["avg_combined"] else 0
        rng_avg = rng_rows[0]["avg_combined"] if rng_rows and rng_rows[0]["avg_combined"] else 0

        ab_cnt = q(cur, "SELECT COUNT(*) as c FROM ladder_snapshots WHERE series_ticker = ? AND yes_ask > 0 AND no_ask > 0", (ab,))
        rng_cnt = q(cur, "SELECT COUNT(*) as c FROM ladder_snapshots WHERE series_ticker = ? AND yes_ask > 0 AND no_ask > 0", (rng,))

        w(f"**{ab} vs {rng}:**")
        w(f"  - {ab}: avg combined = {ab_avg:.1f} ({ab_cnt[0]['c']:,} rows)")
        w(f"  - {rng}: avg combined = {rng_avg:.1f} ({rng_cnt[0]['c']:,} rows)")
        if ab_avg and rng_avg:
            w(f"  - Above/below is {'tighter' if ab_avg < rng_avg else 'wider'} by {abs(ab_avg - rng_avg):.1f}c")
        w()

    # =========================================================
    # 8. THE VERDICT
    # =========================================================
    w("## 8. The Verdict")
    w()

    # Gather summary stats per series
    summary = {}
    for s in AB_SERIES:
        hard = q(cur, "SELECT COUNT(*) as c FROM opportunities WHERE series_ticker = ? AND sub_type = 'hard'", (s,))

        # Recompute scan mins for summary
        rows = q(cur, """
            SELECT timestamp, expiry_window, strike, yes_ask, no_ask
            FROM ladder_snapshots
            WHERE series_ticker = ? AND yes_ask > 0 AND no_ask > 0
            ORDER BY timestamp, expiry_window, strike
        """, (s,))
        scans_data = defaultdict(list)
        for r in rows:
            scans_data[(r["timestamp"], r["expiry_window"])].append(r)

        costs = []
        for (ts, ew), strikes in scans_data.items():
            if len(strikes) < 2:
                continue
            best = None
            for i in range(len(strikes)):
                for j in range(i+1, len(strikes)):
                    cross = strikes[i]["yes_ask"] + strikes[j]["no_ask"]
                    if best is None or cross < best:
                        best = cross
            if best:
                costs.append(best)
        costs.sort()

        # Hours
        ts_rows = q(cur, "SELECT MIN(timestamp) as mn, MAX(timestamp) as mx FROM ladder_snapshots WHERE series_ticker = ?", (s,))
        try:
            hours = (datetime.fromisoformat(ts_rows[0]["mx"]) - datetime.fromisoformat(ts_rows[0]["mn"])).total_seconds() / 3600
        except:
            hours = 0

        # Monotonicity violations
        mono_rows = q(cur, """
            SELECT timestamp, expiry_window, strike, yes_ask
            FROM ladder_snapshots
            WHERE series_ticker = ? AND yes_ask > 0
            ORDER BY timestamp, expiry_window, strike
        """, (s,))
        mono_v = 0
        prev_key = None
        prev_ask = None
        for r in mono_rows:
            key = (r["timestamp"], r["expiry_window"])
            if key != prev_key:
                prev_key = key
                prev_ask = r["yes_ask"]
                continue
            if r["yes_ask"] > prev_ask:
                mono_v += 1
            prev_ask = r["yes_ask"]

        # Median per-strike spread
        spread_rows = q(cur, """
            SELECT yes_ask + no_ask as combined
            FROM ladder_snapshots
            WHERE series_ticker = ? AND yes_ask > 0 AND no_ask > 0
            ORDER BY combined
        """, (s,))
        spreads = [r["combined"] for r in spread_rows]
        med_spread = spreads[len(spreads)//2] if spreads else 0

        n = len(costs)
        summary[s] = {
            "hours": hours,
            "hard_arbs": hard[0]["c"],
            "closest": costs[0] if costs else "N/A",
            "median_min": costs[n//2] if costs else "N/A",
            "below_105": sum(1 for c in costs if c < 105),
            "below_102": sum(1 for c in costs if c < 102),
            "mono_violations": mono_v,
            "med_spread": med_spread,
        }

    w("### Summary Table")
    w()
    w("| Parameter | KXBTCD | KXETHD | KXSOLD |")
    w("|-----------|--------|--------|--------|")
    for label, key in [
        ("Hours scanned", "hours"),
        ("Hard arbs found", "hard_arbs"),
        ("Closest combined cost", "closest"),
        ("Median min combined", "median_min"),
        ("Times below 105", "below_105"),
        ("Times below 102", "below_102"),
        ("Monotonicity violations", "mono_violations"),
        ("Median per-strike spread", "med_spread"),
    ]:
        vals = []
        for s in AB_SERIES:
            v = summary[s][key]
            if key == "hours":
                vals.append(f"{v:.1f}")
            else:
                vals.append(str(v))
        w(f"| {label} | {vals[0]} | {vals[1]} | {vals[2]} |")
    w()

    w("### Answers")
    w()
    w(f"**a) Did any hard arbs appear on above/below contracts?** {'YES' if total_hard > 0 else 'NO'}")
    w()
    if total_hard > 0:
        w(f"**b) Frequency/spreads/depth:** {total_hard} total detections — see Section 3 for details.")
    else:
        w("**b) N/A — zero hard arbs.**")
    w()

    # Assess closeness
    all_closest = []
    for s in AB_SERIES:
        v = summary[s]["closest"]
        if isinstance(v, (int, float)):
            all_closest.append(v)
    best_ever = min(all_closest) if all_closest else "N/A"
    all_below_102 = sum(summary[s]["below_102"] for s in AB_SERIES)
    all_below_105 = sum(summary[s]["below_105"] for s in AB_SERIES)

    w(f"**c) How close did the market get?** Closest combined cost: **{best_ever}**. Below 102: {all_below_102} times. Below 105: {all_below_105} times.")
    w()

    if isinstance(best_ever, (int, float)):
        if best_ever < 100:
            w("**d) Realistic path?** Hard arbs ARE appearing — proceed to execution analysis.")
        elif best_ever < 102:
            w("**d) Realistic path?** Market approaches arb territory. Volatile conditions or thin liquidity moments could produce actionable arbs. Worth monitoring longer.")
        elif best_ever < 105:
            w("**d) Realistic path?** Market gets within 5c of arb. Possible in extreme volatility but not a reliable strategy.")
        else:
            w("**d) Realistic path?** Market stays well above arb territory. Cross-strike arbs are unlikely on these contracts.")
    w()

    # Efficiency comparison
    w("**e) Above/below vs range efficiency:** See Section 7. Above/below contracts have structured pricing (combined ~100-110) vs range contracts (combined ~400-500). Above/below are inherently closer to arbable but still efficiently priced by market makers.")
    w()

    w("### Recommendation")
    w()
    if total_hard > 0 and all_below_102 > 10:
        w("**PROCEED TO PHASE 2** — Hard arbs exist with meaningful frequency.")
    elif all_below_102 > 5:
        w("**MONITOR LONGER** — Near-misses below 102 are appearing. More data during volatile conditions (FOMC, macro events) may reveal actionable arbs. Extend monitoring to 7 days.")
    elif all_below_105 > 20:
        w("**PIVOT TO CONVERGENCE** — Market approaches but doesn't breach arb territory. Consider near-expiry convergence plays or spread-tightening strategies.")
    elif any(summary[s]["med_spread"] > 108 for s in AB_SERIES):
        w("**PIVOT TO MARKET MAKING** — Wide per-strike spreads suggest opportunity to post tighter quotes and earn the spread, rather than cross-strike arb.")
    else:
        w("**STOP CROSS-STRIKE STRATEGY** — The market is efficiently priced. Cross-strike arbs do not exist on above/below contracts with current liquidity conditions. Consider: (1) market making, (2) event-driven directional trading, (3) cross-platform arb vs Polymarket/dYdX.")

    w()
    w("---")
    w(f"*Report generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')} by Phase 1 AB analysis script.*")

    report = "\n".join(lines)
    with open(OUT, "w") as f:
        f.write(report)
    print(f"Report written to {OUT}")
    print(f"Length: {len(lines)} lines")

if __name__ == "__main__":
    run()
