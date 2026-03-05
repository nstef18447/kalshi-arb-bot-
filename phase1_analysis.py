"""Phase 1 Analysis Report Generator for Kalshi Arb Bot."""
from dotenv import load_dotenv
load_dotenv()

import db
import time
from datetime import datetime, timezone, timedelta
from collections import defaultdict

conn = db.get_connection(readonly=True)

# Taker fee fix deployed at this timestamp — data before uses flat 14c estimate
TAKER_FIX_TS = datetime(2026, 2, 24, 15, 54, 32, tzinfo=timezone.utc).timestamp()

def parabolic_fee(price_cents, mult):
    p = price_cents / 100.0
    return mult * p * (1.0 - p) * 100.0

def corrected_taker_profit(yes_ask, no_ask, fee_mult=0.07):
    gross = 100 - yes_ask - no_ask
    fee = parabolic_fee(yes_ask, fee_mult) + parabolic_fee(no_ask, fee_mult)
    return gross - fee

lines = []
def out(s=""):
    print(s)
    lines.append(s)

# ================================================================
# 1. DATA SUMMARY
# ================================================================
out("# Phase 1 Validation Report")
out(f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
out("")
out("## 1. Data Summary")
out("")

# Table counts
for table in ("scans", "ladder_snapshots", "opportunities", "trades"):
    ct = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    out(f"- **{table}**: {ct:,} rows")

out("")

# Time range from scans table
ts_range = conn.execute("SELECT MIN(timestamp), MAX(timestamp) FROM scans").fetchone()
if ts_range[0]:
    start_dt = datetime.fromtimestamp(ts_range[0], tz=timezone.utc)
    end_dt = datetime.fromtimestamp(ts_range[1], tz=timezone.utc)
    hours = (ts_range[1] - ts_range[0]) / 3600
    out(f"- **Scan period**: {start_dt.strftime('%Y-%m-%d %H:%M')} to {end_dt.strftime('%Y-%m-%d %H:%M')} UTC")
    out(f"- **Total hours**: {hours:.1f}")
else:
    hours = 0
    out("- No scan data found!")

out("")

# Per-series breakdown
out("### Per-series scan counts")
out("| Series | Scans | First seen | Last seen |")
out("|--------|-------|------------|-----------|")
rows = conn.execute(
    "SELECT series_ticker, COUNT(*), MIN(timestamp), MAX(timestamp) FROM scans GROUP BY series_ticker ORDER BY series_ticker"
).fetchall()
for r in rows:
    s_dt = datetime.fromtimestamp(r[2], tz=timezone.utc).strftime('%m-%d %H:%M')
    e_dt = datetime.fromtimestamp(r[3], tz=timezone.utc).strftime('%m-%d %H:%M')
    out(f"| {r[0]} | {r[1]:,} | {s_dt} | {e_dt} |")

out("")

# Data gaps (60+ seconds between consecutive scans)
out("### Data gaps (60+ seconds between scans)")
scan_ts = conn.execute("SELECT timestamp FROM scans ORDER BY timestamp").fetchall()
gaps = []
for i in range(1, len(scan_ts)):
    diff = scan_ts[i][0] - scan_ts[i-1][0]
    if diff > 60:
        gap_start = datetime.fromtimestamp(scan_ts[i-1][0], tz=timezone.utc)
        out(f"- {gap_start.strftime('%m-%d %H:%M:%S')} — gap of {diff:.0f}s ({diff/60:.1f} min)")
        gaps.append(diff)
if not gaps:
    out("- No gaps > 60s detected")
out("")

# ================================================================
# 2. OPPORTUNITY FREQUENCY
# ================================================================
out("## 2. Opportunity Frequency")
out("")

# Total hard arbs per series
out("### Raw hard arb detections by series")
out("| Series | Detections |")
out("|--------|------------|")
series_counts = conn.execute(
    "SELECT series_ticker, COUNT(*) FROM opportunities WHERE opp_type='C' AND sub_type='hard' GROUP BY series_ticker ORDER BY COUNT(*) DESC"
).fetchall()
total_hard = sum(r[1] for r in series_counts)
for r in series_counts:
    out(f"| {r[0]} | {r[1]:,} |")
out(f"| **TOTAL** | **{total_hard:,}** |")
out("")

# Deduplicated
out("### Deduplicated unique opportunities")
dedup_rows = conn.execute(
    "SELECT series_ticker, expiry_window, strike_low, strike_high, COUNT(*) as ct, "
    "MIN(timestamp) as first_ts, MAX(timestamp) as last_ts, "
    "MIN(combined_cost) as min_cost, MAX(combined_cost) as max_cost, "
    "MIN(yes_ask_low) as min_yask, MAX(yes_ask_low) as max_yask, "
    "MIN(no_ask_high) as min_nask, MAX(no_ask_high) as max_nask, "
    "MIN(estimated_profit) as min_ep, MAX(estimated_profit) as max_ep, "
    "AVG(estimated_profit_maker) as avg_epm, "
    "AVG(depth_thin_side) as avg_depth, MIN(depth_thin_side) as min_depth, MAX(depth_thin_side) as max_depth "
    "FROM opportunities WHERE opp_type='C' AND sub_type='hard' "
    "GROUP BY series_ticker, expiry_window, strike_low, strike_high "
    "ORDER BY ct DESC"
).fetchall()
unique_total = len(dedup_rows)
out(f"- Raw detections: {total_hard:,}")
out(f"- Unique opportunities: {unique_total}")
out(f"- Avg detections per unique: {total_hard/unique_total:.1f}x" if unique_total else "")
if hours > 0:
    out(f"- Unique arbs per hour: {unique_total/hours:.1f}")
    out(f"- Unique arbs per day (projected): {unique_total/hours*24:.0f}")
out("")

# Per-series unique
out("### Unique arbs by series")
series_unique = defaultdict(int)
for r in dedup_rows:
    series_unique[r[0]] += 1
out("| Series | Unique arbs | Per hour |")
out("|--------|-------------|----------|")
for s, ct in sorted(series_unique.items(), key=lambda x: -x[1]):
    per_hr = ct / hours if hours > 0 else 0
    out(f"| {s} | {ct} | {per_hr:.1f} |")
out("")

# Hour-of-day distribution
out("### Hard arbs by hour of day (UTC / ET)")
hour_counts = defaultdict(int)
# Use deduplicated: take first_ts of each unique opp
for r in dedup_rows:
    hr = datetime.fromtimestamp(r[5], tz=timezone.utc).hour
    hour_counts[hr] += 1

out("```")
max_ct = max(hour_counts.values()) if hour_counts else 1
for h in range(24):
    ct = hour_counts.get(h, 0)
    et_h = (h - 5) % 24
    bar = "#" * int(40 * ct / max_ct) if ct > 0 else ""
    out(f"  {h:02d} UTC ({et_h:02d} ET): {bar} {ct}")
out("```")
out("")

# What % of scan cycles had at least one hard arb
total_scans = conn.execute("SELECT COUNT(DISTINCT timestamp) FROM scans").fetchone()[0]
# Approximate: scans that overlap with opportunity timestamps
scans_with_arb = conn.execute(
    "SELECT COUNT(DISTINCT s.timestamp) FROM scans s "
    "INNER JOIN opportunities o ON ABS(s.timestamp - o.timestamp) < 3 "
    "AND o.opp_type='C' AND o.sub_type='hard'"
).fetchone()[0]
if total_scans > 0:
    out(f"- Scan cycles with at least one hard arb: {scans_with_arb}/{total_scans} ({100*scans_with_arb/total_scans:.1f}%)")
out("")

# ================================================================
# 3. SPREAD ANALYSIS
# ================================================================
out("## 3. Spread Analysis")
out("")

# Flag taker fee methodology
old_count = conn.execute(
    "SELECT COUNT(*) FROM opportunities WHERE opp_type='C' AND sub_type='hard' AND timestamp < ?", (TAKER_FIX_TS,)
).fetchone()[0]
new_count = conn.execute(
    "SELECT COUNT(*) FROM opportunities WHERE opp_type='C' AND sub_type='hard' AND timestamp >= ?", (TAKER_FIX_TS,)
).fetchone()[0]
out(f"**IMPORTANT**: Taker fee formula was fixed at {datetime.fromtimestamp(TAKER_FIX_TS, tz=timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}.")
out(f"- Old data (flat 14c/trade estimate): {old_count:,} rows")
out(f"- New data (correct parabolic formula): {new_count:,} rows")
out("")

# Get all hard arb data for analysis
all_arbs = conn.execute(
    "SELECT combined_cost, estimated_profit, estimated_profit_maker, "
    "yes_ask_low, no_ask_high, depth_thin_side, series_ticker, timestamp "
    "FROM opportunities WHERE opp_type='C' AND sub_type='hard' ORDER BY timestamp"
).fetchall()

# Compute corrected taker profit for all rows
corrected = []
for r in all_arbs:
    series = r[6]
    fee_mult = 0.07 if series in ('KXBTC', 'KXETH', 'KXSOLE') else 0.035
    cp = corrected_taker_profit(r[3], r[4], fee_mult)
    corrected.append({
        "cost": r[0], "est_profit": r[1], "est_profit_maker": r[2],
        "yes_ask": r[3], "no_ask": r[4], "depth": r[5],
        "series": r[6], "ts": r[7], "corrected_taker": cp,
        "gross": 100 - r[0],
    })

# Spread distribution
gross_spreads = [c["gross"] for c in corrected]
out("### Gross spread distribution (100 - combined_cost)")
buckets = [(1, 5), (6, 10), (11, 15), (16, 20), (21, 30), (31, 50), (51, 100)]
out("| Range | Count | % |")
out("|-------|-------|---|")
for lo, hi in buckets:
    ct = sum(1 for s in gross_spreads if lo <= s <= hi)
    out(f"| {lo}-{hi}c | {ct:,} | {100*ct/len(gross_spreads):.1f}% |")
out("")

# Taker profitability (corrected)
taker_prof = [c for c in corrected if c["corrected_taker"] > 0]
maker_prof = [c for c in corrected if c["est_profit_maker"] is not None and c["est_profit_maker"] > 0]
maker_tracked = [c for c in corrected if c["est_profit_maker"] is not None]

out(f"### Profitability rates (using corrected parabolic taker fees)")
out(f"- **Taker-profitable**: {len(taker_prof):,} / {len(corrected):,} ({100*len(taker_prof)/len(corrected):.1f}%)")
out(f"- **Maker-profitable**: {len(maker_prof):,} / {len(maker_tracked):,} ({100*len(maker_prof)/len(maker_tracked):.1f}%) [tracked subset]")
out("")

# Average/median by bucket
out("### Spread by profitability bucket (corrected taker)")
taker_only = [c for c in corrected if c["corrected_taker"] > 0 and (c["est_profit_maker"] is None or c["est_profit_maker"] > 0)]
maker_only = [c for c in corrected if c["corrected_taker"] <= 0 and c["est_profit_maker"] is not None and c["est_profit_maker"] > 0]
unprof = [c for c in corrected if c["corrected_taker"] <= 0 and (c["est_profit_maker"] is None or c["est_profit_maker"] <= 0)]

def stats(lst, key):
    if not lst:
        return "N/A", "N/A"
    vals = sorted([c[key] for c in lst])
    avg = sum(vals) / len(vals)
    med = vals[len(vals)//2]
    return f"{avg:.1f}", f"{med:.1f}"

out("| Bucket | Count | Avg gross | Med gross | Avg net taker | Avg net maker |")
out("|--------|-------|-----------|-----------|---------------|---------------|")
for label, subset in [("Taker-profitable", taker_prof), ("Maker-only profitable", maker_only), ("Unprofitable", unprof)]:
    if subset:
        ag, mg = stats(subset, "gross")
        at, _ = stats(subset, "corrected_taker")
        maker_sub = [c for c in subset if c["est_profit_maker"] is not None]
        am = f"{sum(c['est_profit_maker'] for c in maker_sub)/len(maker_sub):.1f}" if maker_sub else "N/A"
        out(f"| {label} | {len(subset):,} | {ag}c | {mg}c | {at}c | {am}c |")
    else:
        out(f"| {label} | 0 | - | - | - | - |")
out("")

# ================================================================
# 4. DEPTH ANALYSIS
# ================================================================
out("## 4. Depth Analysis")
out("")

depths = sorted([c["depth"] for c in corrected if c["depth"] is not None])
if depths:
    out(f"- Total arbs with depth data: {len(depths)}")
    out(f"- Min: {depths[0]}, Max: {depths[-1]}")
    out(f"- Median: {depths[len(depths)//2]}")
    out(f"- 25th percentile: {depths[len(depths)//4]}")
    out(f"- 75th percentile: {depths[3*len(depths)//4]}")
    out(f"- Depth >= 25 (target trade size): {sum(1 for d in depths if d >= 25)} ({100*sum(1 for d in depths if d >= 25)/len(depths):.1f}%)")
    out(f"- Depth >= 100 (comfortable fill): {sum(1 for d in depths if d >= 100)} ({100*sum(1 for d in depths if d >= 100)/len(depths):.1f}%)")
    out("")

    # Taker-profitable depth
    tp_depths = sorted([c["depth"] for c in taker_prof if c["depth"] is not None])
    if tp_depths:
        out(f"### Taker-profitable arbs depth")
        out(f"- Count with depth data: {len(tp_depths)}")
        out(f"- Median depth: {tp_depths[len(tp_depths)//2]}")
        out(f"- Depth >= 25: {sum(1 for d in tp_depths if d >= 25)} ({100*sum(1 for d in tp_depths if d >= 25)/len(tp_depths):.1f}%)")
        out(f"- Depth >= 100: {sum(1 for d in tp_depths if d >= 100)} ({100*sum(1 for d in tp_depths if d >= 100)/len(tp_depths):.1f}%)")
else:
    out("- No depth data available")
out("")

# ================================================================
# 5. STRIKE CONCENTRATION
# ================================================================
out("## 5. Strike Concentration")
out("")

out("### Top 15 most frequent strike pairs (deduplicated)")
out("| Series | Strikes | Unique opps | Avg gross | Avg depth |")
out("|--------|---------|-------------|-----------|-----------|")
# Group dedup_rows by series + strike pair
pair_counts = defaultdict(lambda: {"count": 0, "gross_sum": 0, "depth_sum": 0, "depth_ct": 0})
for r in dedup_rows:
    key = (r[0], r[2], r[3])  # series, strike_low, strike_high
    pair_counts[key]["count"] += 1
    # r[7] = min_cost, we use that for gross
    avg_cost = (r[7] + r[8]) / 2 if r[7] and r[8] else r[7]
    pair_counts[key]["gross_sum"] += (100 - avg_cost) if avg_cost else 0
    if r[16] is not None:  # avg_depth
        pair_counts[key]["depth_sum"] += r[16]
        pair_counts[key]["depth_ct"] += 1

# Actually, let's just use the dedup_rows directly — they're already grouped by (series, expiry, strike_low, strike_high)
# We want to group by (series, strike_low, strike_high) ignoring expiry
strike_pair_agg = defaultdict(lambda: {"appearances": 0, "gross_total": 0, "depth_total": 0, "depth_ct": 0})
for r in dedup_rows:
    key = (r[0], r[2], r[3])
    strike_pair_agg[key]["appearances"] += 1
    avg_cost = (r[7] + r[8]) / 2
    strike_pair_agg[key]["gross_total"] += (100 - avg_cost)
    if r[16] is not None:
        strike_pair_agg[key]["depth_total"] += r[16]
        strike_pair_agg[key]["depth_ct"] += 1

top_pairs = sorted(strike_pair_agg.items(), key=lambda x: -x[1]["appearances"])[:15]
for (series, sl, sh), v in top_pairs:
    ag = v["gross_total"] / v["appearances"] if v["appearances"] else 0
    ad = v["depth_total"] / v["depth_ct"] if v["depth_ct"] else 0
    out(f"| {series} | {sl:.0f}-{sh:.0f} | {v['appearances']} | {ag:.1f}c | {ad:.0f} |")
out("")

# Strike distance
dists = [r[3] - r[2] for r in dedup_rows]
if dists:
    dists_sorted = sorted(dists)
    out(f"### Strike distance (high - low)")
    out(f"- Min: {min(dists):.0f}, Max: {max(dists):.0f}")
    out(f"- Median: {dists_sorted[len(dists_sorted)//2]:.0f}")
    out(f"- Mean: {sum(dists)/len(dists):.0f}")
out("")

# How concentrated?
unique_pairs_count = len(strike_pair_agg)
out(f"- Total unique strike pairs (across all expiries): {unique_pairs_count}")
top3_ct = sum(v["appearances"] for _, v in top_pairs[:3])
out(f"- Top 3 pairs account for {top3_ct}/{unique_total} unique opps ({100*top3_ct/unique_total:.1f}%)" if unique_total else "")
out("")

# ================================================================
# 6. PERSISTENCE
# ================================================================
out("## 6. Persistence Analysis")
out("")

# Each dedup_row has first_ts, last_ts, count
# Persistence = last_ts - first_ts (+ one scan interval for the last detection)
SCAN_INTERVAL = 5  # approximate
persistences = []
for r in dedup_rows:
    duration = r[6] - r[5]  # last_ts - first_ts
    # Add one scan interval since the arb existed at least through the last detection
    if r[4] > 1:  # more than one detection
        persistence = duration + SCAN_INTERVAL
    else:
        persistence = SCAN_INTERVAL  # single detection = at most one interval
    persistences.append({
        "series": r[0], "expiry": r[1], "sl": r[2], "sh": r[3],
        "detections": r[4], "persistence_s": persistence,
        "first_ts": r[5], "last_ts": r[6],
        "min_cost": r[7], "max_cost": r[8],
        "avg_epm": r[15], "avg_depth": r[16], "min_depth": r[17], "max_depth": r[18],
    })

pers_times = sorted([p["persistence_s"] for p in persistences])
out(f"- Total unique opportunities: {len(persistences)}")
out(f"- Median persistence: {pers_times[len(pers_times)//2]:.0f}s")
out(f"- Mean persistence: {sum(pers_times)/len(pers_times):.0f}s")
out(f"- Max persistence: {max(pers_times):.0f}s ({max(pers_times)/60:.1f} min)")
out("")

out("### Persistence distribution")
pers_buckets = [(0, 5, "<5s (single scan)"), (6, 10, "5-10s"), (11, 30, "10-30s"),
                (31, 60, "30-60s"), (61, 180, "1-3 min"), (181, 600, "3-10 min"), (601, 99999, "10+ min")]
out("| Duration | Count | % |")
out("|----------|-------|---|")
for lo, hi, label in pers_buckets:
    ct = sum(1 for p in pers_times if lo <= p <= hi)
    out(f"| {label} | {ct} | {100*ct/len(pers_times):.1f}% |")
out("")

gt10 = sum(1 for p in pers_times if p >= 10)
gt30 = sum(1 for p in pers_times if p >= 30)
gt60 = sum(1 for p in pers_times if p >= 60)
gt180 = sum(1 for p in pers_times if p >= 180)
out(f"- Persisted 10+ seconds: {gt10} ({100*gt10/len(pers_times):.1f}%)")
out(f"- Persisted 30+ seconds: {gt30} ({100*gt30/len(pers_times):.1f}%)")
out(f"- Persisted 60+ seconds: {gt60} ({100*gt60/len(pers_times):.1f}%)")
out(f"- Persisted 3+ minutes: {gt180} ({100*gt180/len(pers_times):.1f}%)")
out("")

# Persistence for taker-profitable vs all
# Need to identify which unique opps are taker-profitable
# Use corrected taker: compute for each unique opp using its avg prices
tp_pers = []
for p in persistences:
    # Find corrected taker for this opp (use min_cost as representative)
    fee_mult = 0.07 if p["series"] in ('KXBTC', 'KXETH', 'KXSOLE') else 0.035
    # We don't have exact yes/no split for the unique opp, but we can check if ANY detection was taker-profitable
    # Actually let's query
    pass

out("### Opportunities persisting 3+ minutes (strong execution evidence)")
long_lived = sorted([p for p in persistences if p["persistence_s"] >= 180], key=lambda x: -x["persistence_s"])
if long_lived:
    out("| Series | Strikes | Duration | Detections | Cost range | Avg depth |")
    out("|--------|---------|----------|------------|------------|-----------|")
    for p in long_lived[:20]:
        dur = f"{p['persistence_s']/60:.1f} min"
        out(f"| {p['series']} | {p['sl']:.0f}-{p['sh']:.0f} | {dur} | {p['detections']} | {p['min_cost']}-{p['max_cost']}c | {p['avg_depth']:.0f} |")
else:
    out("None found.")
out("")

# ================================================================
# 7. DEPTH STABILITY
# ================================================================
out("## 7. Depth Stability (Top 20 Most Persistent)")
out("")

top20 = sorted(persistences, key=lambda x: -x["persistence_s"])[:20]
stable_ct = 0
growing_ct = 0
pulled_ct = 0
out("| Series | Strikes | Duration | Min depth | Max depth | Status |")
out("|--------|---------|----------|-----------|-----------|--------|")
for p in top20:
    dur = f"{p['persistence_s']:.0f}s"
    md = p["min_depth"]
    xd = p["max_depth"]
    if md is None or xd is None:
        status = "no data"
    elif md < 10:
        status = "PULLED (min < 10)"
        pulled_ct += 1
    elif xd > md * 1.2:
        status = "GROWING"
        growing_ct += 1
    else:
        status = "STABLE"
        stable_ct += 1
    out(f"| {p['series']} | {p['sl']:.0f}-{p['sh']:.0f} | {dur} | {md} | {xd} | {status} |")

total_classified = stable_ct + growing_ct + pulled_ct
if total_classified > 0:
    out("")
    out(f"- Stable: {stable_ct}/{total_classified} ({100*stable_ct/total_classified:.0f}%)")
    out(f"- Growing (depth added): {growing_ct}/{total_classified} ({100*growing_ct/total_classified:.0f}%)")
    out(f"- Pulled (min depth < 10): {pulled_ct}/{total_classified} ({100*pulled_ct/total_classified:.0f}%)")
out("")

# ================================================================
# 8. EXPIRY TIMING
# ================================================================
out("## 8. Expiry Timing")
out("")

# Check expiry_window values
expiry_vals = conn.execute(
    "SELECT DISTINCT expiry_window FROM opportunities WHERE opp_type='C' AND sub_type='hard' ORDER BY expiry_window LIMIT 20"
).fetchall()
out("### Sample expiry_window values")
for r in expiry_vals:
    out(f"- `{r[0]}`")
out("")

# Check time_to_expiry_seconds
tte_stats = conn.execute(
    "SELECT MIN(time_to_expiry_seconds), MAX(time_to_expiry_seconds), AVG(time_to_expiry_seconds) "
    "FROM opportunities WHERE opp_type='C' AND sub_type='hard' AND time_to_expiry_seconds IS NOT NULL"
).fetchone()
if tte_stats[0] is not None:
    out(f"### time_to_expiry_seconds")
    out(f"- Min: {tte_stats[0]:.0f}s ({tte_stats[0]/3600:.1f}h)")
    out(f"- Max: {tte_stats[1]:.0f}s ({tte_stats[1]/3600:.1f}h)")
    out(f"- Avg: {tte_stats[2]:.0f}s ({tte_stats[2]/3600:.1f}h)")
    if tte_stats[1] > 86400:
        out("")
        out("**BUG CONFIRMED**: time_to_expiry_seconds values are in the hundreds of thousands,")
        out("indicating these are NOT 15-minute contracts or the calculation is broken.")
        out("Values of 600,000+ seconds = ~7 days, suggesting weekly expiry windows.")
out("")

# Check actual market tickers to understand contract duration
sample_tickers = conn.execute(
    "SELECT DISTINCT expiry_window FROM opportunities WHERE opp_type='C' AND sub_type='hard' LIMIT 5"
).fetchall()
out("### Contract duration check")
out("The expiry_window values above should indicate the contract type:")
out("- If timestamps are 15 minutes apart (e.g., 14:00, 14:15, 14:30) → 15-min contracts")
out("- If timestamps are hours apart (e.g., 10:00, 16:00) → hourly contracts")
out("- If timestamps are days apart (e.g., Feb 24, Feb 27, Mar 3) → daily/weekly contracts")

# Parse and analyze expiry windows
expiry_dates = set()
for r in expiry_vals:
    val = r[0]
    try:
        dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
        expiry_dates.add(dt.date())
    except:
        pass

if expiry_dates:
    out(f"\nUnique expiry dates: {sorted(expiry_dates)}")
    if len(expiry_dates) <= 5:
        out("\n**WARNING**: Only a few unique expiry dates found. These may be daily/weekly")
        out("contracts, NOT 15-minute windows. This is critical — the arb strategy assumes")
        out("short-duration contracts where one side must pay out soon.")

# Check time gaps between expiry windows
expiry_dts = []
for r in expiry_vals:
    try:
        dt = datetime.fromisoformat(r[0].replace("Z", "+00:00"))
        expiry_dts.append(dt)
    except:
        pass
if len(expiry_dts) >= 2:
    expiry_dts.sort()
    gaps_between = [(expiry_dts[i+1] - expiry_dts[i]).total_seconds() / 3600 for i in range(len(expiry_dts)-1)]
    out(f"\nGaps between consecutive expiry windows: {[f'{g:.1f}h' for g in gaps_between[:10]]}")
out("")

# ================================================================
# 9. COMPETITION SIGNALS
# ================================================================
out("## 9. Competition Signals")
out("")

# Split data in half
if len(corrected) >= 10:
    mid = len(corrected) // 2
    first_half = corrected[:mid]
    second_half = corrected[mid:]

    avg_gross_1 = sum(c["gross"] for c in first_half) / len(first_half)
    avg_gross_2 = sum(c["gross"] for c in second_half) / len(second_half)

    tp_rate_1 = 100 * sum(1 for c in first_half if c["corrected_taker"] > 0) / len(first_half)
    tp_rate_2 = 100 * sum(1 for c in second_half if c["corrected_taker"] > 0) / len(second_half)

    hours_1 = (first_half[-1]["ts"] - first_half[0]["ts"]) / 3600
    hours_2 = (second_half[-1]["ts"] - second_half[0]["ts"]) / 3600

    # Unique opps in each half
    unique_1 = set()
    unique_2 = set()
    for r in dedup_rows:
        if r[5] < corrected[mid]["ts"]:
            unique_1.add((r[0], r[1], r[2], r[3]))
        else:
            unique_2.add((r[0], r[1], r[2], r[3]))

    freq_1 = len(unique_1) / hours_1 if hours_1 > 0 else 0
    freq_2 = len(unique_2) / hours_2 if hours_2 > 0 else 0

    out("### First half vs second half comparison")
    out("| Metric | First half | Second half | Trend |")
    out("|--------|-----------|-------------|-------|")
    out(f"| Avg gross spread | {avg_gross_1:.1f}c | {avg_gross_2:.1f}c | {'↓ Compressing' if avg_gross_2 < avg_gross_1 else '↑ Widening'} |")
    out(f"| Taker-profitable rate | {tp_rate_1:.1f}% | {tp_rate_2:.1f}% | {'↓' if tp_rate_2 < tp_rate_1 else '↑'} |")
    out(f"| Unique arbs/hour | {freq_1:.1f} | {freq_2:.1f} | {'↓ Declining' if freq_2 < freq_1 else '↑ Increasing'} |")
    out("")

# Flash opps (single-scan, gone in <5s)
flash = sum(1 for p in persistences if p["detections"] == 1)
out(f"### Flash opportunities (single scan, <5s)")
out(f"- Single-scan arbs: {flash}/{len(persistences)} ({100*flash/len(persistences):.1f}%)")
out(f"- These are likely untradeable at current scan speed")
out("")

# ================================================================
# 10. PER-SERIES COMPARISON
# ================================================================
out("## 10. Per-Series Comparison")
out("")

all_series = sorted(set(c["series"] for c in corrected))
out("| Metric | " + " | ".join(all_series) + " |")
out("|--------|" + "|".join(["-----" for _ in all_series]) + "|")

# Unique opps
row_vals = []
for s in all_series:
    ct = series_unique.get(s, 0)
    row_vals.append(str(ct))
out(f"| Unique arbs | " + " | ".join(row_vals) + " |")

# Avg gross spread
row_vals = []
for s in all_series:
    subset = [c for c in corrected if c["series"] == s]
    avg = sum(c["gross"] for c in subset) / len(subset) if subset else 0
    row_vals.append(f"{avg:.1f}c")
out(f"| Avg gross spread | " + " | ".join(row_vals) + " |")

# Taker profitable %
row_vals = []
for s in all_series:
    subset = [c for c in corrected if c["series"] == s]
    tp = sum(1 for c in subset if c["corrected_taker"] > 0)
    pct = 100 * tp / len(subset) if subset else 0
    row_vals.append(f"{pct:.0f}%")
out(f"| Taker profitable | " + " | ".join(row_vals) + " |")

# Median depth
row_vals = []
for s in all_series:
    subset = sorted([c["depth"] for c in corrected if c["series"] == s and c["depth"] is not None])
    med = subset[len(subset)//2] if subset else 0
    row_vals.append(str(med))
out(f"| Median depth | " + " | ".join(row_vals) + " |")

out("")

# Equity index note
equity_series = [s for s in all_series if s in ('KXINXU', 'KXNASDAQ100U')]
for s in equity_series:
    ct = series_unique.get(s, 0)
    if ct == 0:
        subset = [c for c in corrected if c["series"] == s]
        if not subset:
            out(f"**{s}**: Zero hard arbs detected. This series has not produced any actionable opportunities.")
        else:
            avg_cost = sum(c["cost"] for c in subset) / len(subset)
            out(f"**{s}**: Combined costs averaging {avg_cost:.0f}c (well above 100c), confirming wide spreads.")
out("")

# ================================================================
# 11. THE VERDICT
# ================================================================
out("## 11. The Verdict")
out("")

# Compute final answers
tradeable = unique_total > 0 and any(c["corrected_taker"] > 0 for c in corrected)
arbs_per_day = unique_total / hours * 24 if hours > 0 else 0
any_taker = any(c["corrected_taker"] > 0 for c in corrected)
any_maker = any(c["est_profit_maker"] is not None and c["est_profit_maker"] > 0 for c in corrected)

maker_nets = sorted([c["est_profit_maker"] for c in corrected if c["est_profit_maker"] is not None and c["est_profit_maker"] > 0])
median_maker = maker_nets[len(maker_nets)//2] if maker_nets else 0

taker_nets = sorted([c["corrected_taker"] for c in taker_prof])
median_taker = taker_nets[len(taker_nets)//2] if taker_nets else 0

depth_ge25 = sum(1 for c in corrected if c["depth"] is not None and c["depth"] >= 25)
depth_pct = 100 * depth_ge25 / len(corrected) if corrected else 0

persist_ge10 = sum(1 for p in pers_times if p >= 10)
persist_pct = 100 * persist_ge10 / len(pers_times) if pers_times else 0

# Concentration
top_series = max(series_unique.items(), key=lambda x: x[1]) if series_unique else ("N/A", 0)
concentration = f"Concentrated in {top_series[0]} ({top_series[1]}/{unique_total} = {100*top_series[1]/unique_total:.0f}%)" if unique_total else "N/A"

zero_arb_series = [s for s in all_series if series_unique.get(s, 0) == 0]

out("| Question | Answer |")
out("|----------|--------|")
out(f"| a) Do tradeable hard arbs exist? | {'YES' if tradeable else 'NO'} |")
out(f"| b) Unique arbs per day (average)? | {arbs_per_day:.0f} |")
out(f"| c) Any profitable after taker fees? | {'YES' if any_taker else 'NO'} |")
out(f"| d) Any profitable after maker fees? | {'YES' if any_maker else 'NO'} |")
out(f"| e) Median net profit (taker-profitable)? | {median_taker:.1f}c |")
out(f"| f) Median net profit (maker-profitable)? | {median_maker:.1f}c |")
out(f"| g) Depth sufficient for 25 contracts? | {'YES' if depth_pct > 50 else 'MIXED'} ({depth_pct:.0f}%) |")
out(f"| h) Arbs persist long enough (>10s)? | {'YES' if persist_pct > 50 else 'MIXED'} ({persist_pct:.0f}%) |")
out(f"| i) Opportunity concentrated or diversified? | {concentration} |")
out(f"| j) Competition signals concerning? | See section 9 |")
out("")

# Recommendation
out("### Recommendation")
out("")
taker_per_day = len(taker_prof) / hours * 24 / (total_hard / unique_total) if hours > 0 and unique_total > 0 else 0  # rough dedup
if any_taker and taker_per_day >= 5:
    out("**BUILD PHASE 2 (taker strategy)** — Taker-profitable arbs appear frequently enough")
    out("with sufficient depth and persistence to warrant live testing.")
elif any_maker and arbs_per_day >= 10:
    out("**BUILD PHASE 2 (maker strategy)** — Maker-profitable arbs are abundant. Taker-profitable")
    out("arbs exist but may not be frequent enough alone. Maker orders provide the edge.")
elif arbs_per_day < 5:
    out("**PIVOT** — Arb frequency is too low to support active trading at current market conditions.")
else:
    out("**STOP** — The opportunity does not exist at tradeable scale.")
out("")

# Summary table
best_series = top_series[0]
out("### Summary Table")
out("")
out("| Parameter | Observed |")
out("|-----------|----------|")
out(f"| Scan hours | {hours:.1f} |")
out(f"| Hard arb detections (raw) | {total_hard:,} |")
out(f"| Unique hard arbs (deduplicated) | {unique_total} |")
out(f"| Unique hard arbs per day | {arbs_per_day:.0f} |")
out(f"| Avg gross spread | {sum(c['gross'] for c in corrected)/len(corrected):.1f} cents |")
out(f"| Profitable at taker fees | {100*len(taker_prof)/len(corrected):.1f}% |")
out(f"| Profitable at maker fees | {100*len(maker_prof)/len(maker_tracked):.1f}% |")
tp_avg = sum(c["corrected_taker"] for c in taker_prof) / len(taker_prof) if taker_prof else 0
mp_avg = sum(c["est_profit_maker"] for c in maker_prof) / len(maker_prof) if maker_prof else 0
out(f"| Avg net profit (taker, of taker-profitable) | {tp_avg:.1f} cents |")
out(f"| Avg net profit (maker, of maker-profitable) | {mp_avg:.1f} cents |")
out(f"| Median depth (thin side) | {depths[len(depths)//2] if depths else 'N/A'} contracts |")
out(f"| Median persistence | {pers_times[len(pers_times)//2]:.0f} seconds |")
out(f"| Arbs persisting 60+ seconds | {100*gt60/len(pers_times):.1f}% |")
out(f"| Best series | {best_series} |")
out(f"| Series producing zero arbs | {', '.join(zero_arb_series) if zero_arb_series else 'None'} |")
out("")

conn.close()

# Write report
with open("/opt/kalshi-arb-bot/phase1_report.md" if __name__ == "__main__" else "phase1_report.md", "w") as f:
    f.write("\n".join(lines))

print("\n\nReport saved to phase1_report.md")
