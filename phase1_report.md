# Phase 1 Validation Report
Generated: 2026-02-25 04:48 UTC

## 1. Data Summary

- **scans**: 11,676 rows
- **ladder_snapshots**: 136,928 rows
- **opportunities**: 11,248 rows
- **trades**: 0 rows

- **Scan period**: 2026-02-24 01:47 to 2026-02-25 04:48 UTC
- **Total hours**: 27.0

### Per-series scan counts
| Series | Scans | First seen | Last seen |
|--------|-------|------------|-----------|
| KXBTC | 7,434 | 02-24 01:47 | 02-25 04:47 |
| KXETH | 1,491 | 02-24 15:20 | 02-25 04:47 |
| KXINX | 14 | 02-24 14:35 | 02-24 14:38 |
| KXINXU | 641 | 02-24 14:40 | 02-25 04:48 |
| KXNASDAQ100 | 14 | 02-24 14:35 | 02-24 14:39 |
| KXNASDAQ100U | 636 | 02-24 14:41 | 02-25 04:48 |
| KXSOLE | 1,446 | 02-24 15:26 | 02-25 04:47 |

### Data gaps (60+ seconds between scans)
- 02-24 01:53:40 — gap of 464s (7.7 min)
- 02-24 02:18:04 — gap of 245s (4.1 min)
- 02-24 06:34:39 — gap of 69s (1.2 min)
- 02-24 07:50:00 — gap of 66s (1.1 min)
- 02-24 15:24:53 — gap of 62s (1.0 min)

## 2. Opportunity Frequency

### Raw hard arb detections by series
| Series | Detections |
|--------|------------|
| KXBTC | 1,604 |
| KXETH | 13 |
| KXSOLE | 12 |
| **TOTAL** | **1,629** |

### Deduplicated unique opportunities
- Raw detections: 1,629
- Unique opportunities: 232
- Avg detections per unique: 7.0x
- Unique arbs per hour: 8.6
- Unique arbs per day (projected): 206

### Unique arbs by series
| Series | Unique arbs | Per hour |
|--------|-------------|----------|
| KXBTC | 217 | 8.0 |
| KXETH | 8 | 0.3 |
| KXSOLE | 7 | 0.3 |

### Hard arbs by hour of day (UTC / ET)
```
  00 UTC (19 ET): #### 3
  01 UTC (20 ET): ######################### 17
  02 UTC (21 ET): ######################################## 27
  03 UTC (22 ET): ####################### 16
  04 UTC (23 ET): ################# 12
  05 UTC (00 ET): ####### 5
  06 UTC (01 ET): ########## 7
  07 UTC (02 ET): #### 3
  08 UTC (03 ET): # 1
  09 UTC (04 ET): # 1
  10 UTC (05 ET): # 1
  11 UTC (06 ET): ##### 4
  12 UTC (07 ET): ##### 4
  13 UTC (08 ET): ############################# 20
  14 UTC (09 ET): ####################### 16
  15 UTC (10 ET): ################################### 24
  16 UTC (11 ET): ################ 11
  17 UTC (12 ET): ################# 12
  18 UTC (13 ET): ################### 13
  19 UTC (14 ET): ############## 10
  20 UTC (15 ET): ########### 8
  21 UTC (16 ET): ########## 7
  22 UTC (17 ET): ######## 6
  23 UTC (18 ET): ##### 4
```

- Scan cycles with at least one hard arb: 1652/11676 (14.1%)

## 3. Spread Analysis

**IMPORTANT**: Taker fee formula was fixed at 2026-02-24 15:54 UTC.
- Old data (flat 14c/trade estimate): 1,330 rows
- New data (correct parabolic formula): 299 rows

### Gross spread distribution (100 - combined_cost)
| Range | Count | % |
|-------|-------|---|
| 1-5c | 487 | 29.9% |
| 6-10c | 497 | 30.5% |
| 11-15c | 309 | 19.0% |
| 16-20c | 186 | 11.4% |
| 21-30c | 106 | 6.5% |
| 31-50c | 33 | 2.0% |
| 51-100c | 11 | 0.7% |

### Profitability rates (using corrected parabolic taker fees)
- **Taker-profitable**: 1,525 / 1,629 (93.6%)
- **Maker-profitable**: 1,544 / 1,544 (100.0%) [tracked subset]

### Spread by profitability bucket (corrected taker)
| Bucket | Count | Avg gross | Med gross | Avg net taker | Avg net maker |
|--------|-------|-----------|-----------|---------------|---------------|
| Taker-profitable | 1,525 | 11.2c | 9.0c | 9.4c | 11.0c |
| Maker-only profitable | 96 | 1.1c | 1.0c | -0.4c | 0.7c |
| Unprofitable | 8 | 1.0c | 1.0c | -0.6c | N/Ac |

## 4. Depth Analysis

- Total arbs with depth data: 1629
- Min: 1, Max: 20050
- Median: 800
- 25th percentile: 200
- 75th percentile: 1250
- Depth >= 25 (target trade size): 1622 (99.6%)
- Depth >= 100 (comfortable fill): 1531 (94.0%)

### Taker-profitable arbs depth
- Count with depth data: 1525
- Median depth: 800
- Depth >= 25: 1518 (99.5%)
- Depth >= 100: 1430 (93.8%)

## 5. Strike Concentration

### Top 15 most frequent strike pairs (deduplicated)
| Series | Strikes | Unique opps | Avg gross | Avg depth |
|--------|---------|-------------|-----------|-----------|
| KXBTC | 63500-64000 | 8 | 19.6c | 2697 |
| KXBTC | 63750-64000 | 8 | 32.9c | 1775 |
| KXBTC | 63500-64250 | 7 | 15.4c | 2732 |
| KXBTC | 62500-63000 | 7 | 14.6c | 844 |
| KXBTC | 63250-64000 | 6 | 11.0c | 1163 |
| KXBTC | 62250-63000 | 6 | 13.0c | 1065 |
| KXBTC | 63250-63750 | 6 | 15.5c | 1997 |
| KXBTC | 62500-62750 | 6 | 7.9c | 661 |
| KXBTC | 63500-63750 | 6 | 14.6c | 2077 |
| KXBTC | 62750-63000 | 6 | 27.5c | 260 |
| KXBTC | 62500-63250 | 6 | 11.2c | 1020 |
| KXBTC | 62750-63250 | 6 | 14.1c | 496 |
| KXBTC | 63750-64250 | 6 | 19.6c | 2202 |
| KXBTC | 62250-62750 | 5 | 11.3c | 1263 |
| KXBTC | 63250-64250 | 5 | 9.5c | 1571 |

### Strike distance (high - low)
- Min: 2, Max: 6000
- Median: 500
- Mean: 752

- Total unique strike pairs (across all expiries): 93
- Top 3 pairs account for 23/232 unique opps (9.9%)

## 6. Persistence Analysis

- Total unique opportunities: 232
- Median persistence: 421s
- Mean persistence: 727s
- Max persistence: 4877s (81.3 min)

### Persistence distribution
| Duration | Count | % |
|----------|-------|---|
| <5s (single scan) | 64 | 27.6% |
| 5-10s | 0 | 0.0% |
| 10-30s | 2 | 0.9% |
| 30-60s | 2 | 0.9% |
| 1-3 min | 19 | 8.2% |
| 3-10 min | 41 | 17.7% |
| 10+ min | 104 | 44.8% |

- Persisted 10+ seconds: 168 (72.4%)
- Persisted 30+ seconds: 166 (71.6%)
- Persisted 60+ seconds: 164 (70.7%)
- Persisted 3+ minutes: 145 (62.5%)

### Opportunities persisting 3+ minutes (strong execution evidence)
| Series | Strikes | Duration | Detections | Cost range | Avg depth |
|--------|---------|----------|------------|------------|-----------|
| KXBTC | 60750-63250 | 81.3 min | 5 | 98-99c | 1560 |
| KXETH | 1720-1800 | 70.4 min | 3 | 93-94c | 500 |
| KXSOLE | 74-76 | 62.1 min | 5 | 98-99c | 4037 |
| KXBTC | 64000-64500 | 52.8 min | 4 | 90-99c | 1912 |
| KXBTC | 62500-63000 | 48.7 min | 20 | 79-98c | 2696 |
| KXBTC | 64000-64500 | 48.4 min | 8 | 72-98c | 3250 |
| KXBTC | 64000-64250 | 48.4 min | 8 | 31-99c | 2782 |
| KXBTC | 62500-62750 | 48.2 min | 42 | 47-96c | 1746 |
| KXBTC | 62250-62750 | 44.4 min | 69 | 68-98c | 1195 |
| KXBTC | 63750-64000 | 41.8 min | 5 | 27-99c | 2992 |
| KXBTC | 64000-64250 | 41.6 min | 3 | 48-97c | 6857 |
| KXBTC | 62250-62500 | 40.1 min | 32 | 92-99c | 1751 |
| KXBTC | 62250-63000 | 38.9 min | 43 | 76-97c | 2065 |
| KXBTC | 63750-64000 | 38.9 min | 4 | 23-81c | 938 |
| KXBTC | 63250-64250 | 36.1 min | 8 | 90-99c | 3699 |
| KXBTC | 62000-62500 | 34.6 min | 8 | 86-96c | 726 |
| KXBTC | 62000-62750 | 34.2 min | 12 | 78-96c | 870 |
| KXBTC | 62250-62750 | 33.1 min | 51 | 84-98c | 3531 |
| KXBTC | 62000-62750 | 32.9 min | 64 | 80-93c | 1597 |
| KXBTC | 62500-63000 | 31.6 min | 16 | 64-84c | 169 |

## 7. Depth Stability (Top 20 Most Persistent)

| Series | Strikes | Duration | Min depth | Max depth | Status |
|--------|---------|----------|-----------|-----------|--------|
| KXBTC | 60750-63250 | 4877s | 800 | 1750 | GROWING |
| KXETH | 1720-1800 | 4225s | 500 | 500 | STABLE |
| KXSOLE | 74-76 | 3723s | 184 | 5000 | GROWING |
| KXBTC | 64000-64500 | 3166s | 200 | 3250 | GROWING |
| KXBTC | 62500-63000 | 2920s | 47 | 18800 | GROWING |
| KXBTC | 64000-64500 | 2907s | 200 | 18000 | GROWING |
| KXBTC | 64000-64250 | 2907s | 200 | 18000 | GROWING |
| KXBTC | 62500-62750 | 2893s | 100 | 18000 | GROWING |
| KXBTC | 62250-62750 | 2662s | 100 | 12800 | GROWING |
| KXBTC | 63750-64000 | 2507s | 254 | 12392 | GROWING |
| KXBTC | 64000-64250 | 2497s | 220 | 20050 | GROWING |
| KXBTC | 62250-62500 | 2405s | 200 | 14250 | GROWING |
| KXBTC | 62250-63000 | 2334s | 200 | 18800 | GROWING |
| KXBTC | 63750-64000 | 2332s | 500 | 1450 | GROWING |
| KXBTC | 63250-64250 | 2168s | 200 | 19000 | GROWING |
| KXBTC | 62000-62500 | 2073s | 200 | 1750 | GROWING |
| KXBTC | 62000-62750 | 2050s | 200 | 2250 | GROWING |
| KXBTC | 62250-62750 | 1986s | 50 | 19000 | GROWING |
| KXBTC | 62000-62750 | 1973s | 50 | 13000 | GROWING |
| KXBTC | 62500-63000 | 1894s | 50 | 1000 | GROWING |

- Stable: 1/20 (5%)
- Growing (depth added): 19/20 (95%)
- Pulled (min depth < 10): 0/20 (0%)

## 8. Expiry Timing

### Sample expiry_window values
- `2026-03-03T03:00:00Z`
- `2026-03-03T04:00:00Z`
- `2026-03-03T05:00:00Z`
- `2026-03-03T06:00:00Z`
- `2026-03-03T07:00:00Z`
- `2026-03-03T08:00:00Z`
- `2026-03-03T09:00:00Z`
- `2026-03-03T10:00:00Z`
- `2026-03-03T11:00:00Z`
- `2026-03-03T12:00:00Z`
- `2026-03-03T13:00:00Z`
- `2026-03-03T14:00:00Z`
- `2026-03-03T15:00:00Z`
- `2026-03-03T16:00:00Z`
- `2026-03-03T17:00:00Z`
- `2026-03-03T18:00:00Z`
- `2026-03-03T19:00:00Z`
- `2026-03-03T20:00:00Z`
- `2026-03-03T21:00:00Z`
- `2026-03-03T22:00:00Z`

### time_to_expiry_seconds
- Min: 604966s (168.0h)
- Max: 692758s (192.4h)
- Avg: 608421s (169.0h)

**BUG CONFIRMED**: time_to_expiry_seconds values are in the hundreds of thousands,
indicating these are NOT 15-minute contracts or the calculation is broken.
Values of 600,000+ seconds = ~7 days, suggesting weekly expiry windows.

### Contract duration check
The expiry_window values above should indicate the contract type:
- If timestamps are 15 minutes apart (e.g., 14:00, 14:15, 14:30) → 15-min contracts
- If timestamps are hours apart (e.g., 10:00, 16:00) → hourly contracts
- If timestamps are days apart (e.g., Feb 24, Feb 27, Mar 3) → daily/weekly contracts

Unique expiry dates: [datetime.date(2026, 3, 3)]

**WARNING**: Only a few unique expiry dates found. These may be daily/weekly
contracts, NOT 15-minute windows. This is critical — the arb strategy assumes
short-duration contracts where one side must pay out soon.

Gaps between consecutive expiry windows: ['1.0h', '1.0h', '1.0h', '1.0h', '1.0h', '1.0h', '1.0h', '1.0h', '1.0h', '1.0h']

## 9. Competition Signals

### First half vs second half comparison
| Metric | First half | Second half | Trend |
|--------|-----------|-------------|-------|
| Avg gross spread | 10.4c | 10.6c | ↑ Widening |
| Taker-profitable rate | 94.2% | 93.0% | ↓ |
| Unique arbs/hour | 6.1 | 10.7 | ↑ Increasing |

### Flash opportunities (single scan, <5s)
- Single-scan arbs: 64/232 (27.6%)
- These are likely untradeable at current scan speed

## 10. Per-Series Comparison

| Metric | KXBTC | KXETH | KXSOLE |
|--------|-----|-----|-----|
| Unique arbs | 217 | 8 | 7 |
| Avg gross spread | 10.6c | 10.2c | 1.6c |
| Taker profitable | 94% | 100% | 25% |
| Median depth | 800 | 500 | 5000 |


## 11. The Verdict

| Question | Answer |
|----------|--------|
| a) Do tradeable hard arbs exist? | YES |
| b) Unique arbs per day (average)? | 206 |
| c) Any profitable after taker fees? | YES |
| d) Any profitable after maker fees? | YES |
| e) Median net profit (taker-profitable)? | 7.5c |
| f) Median net profit (maker-profitable)? | 8.6c |
| g) Depth sufficient for 25 contracts? | YES (100%) |
| h) Arbs persist long enough (>10s)? | YES (72%) |
| i) Opportunity concentrated or diversified? | Concentrated in KXBTC (217/232 = 94%) |
| j) Competition signals concerning? | See section 9 |

### Recommendation

**BUILD PHASE 2 (taker strategy)** — Taker-profitable arbs appear frequently enough
with sufficient depth and persistence to warrant live testing.

### Summary Table

| Parameter | Observed |
|-----------|----------|
| Scan hours | 27.0 |
| Hard arb detections (raw) | 1,629 |
| Unique hard arbs (deduplicated) | 232 |
| Unique hard arbs per day | 206 |
| Avg gross spread | 10.5 cents |
| Profitable at taker fees | 93.6% |
| Profitable at maker fees | 100.0% |
| Avg net profit (taker, of taker-profitable) | 9.4 cents |
| Avg net profit (maker, of maker-profitable) | 10.3 cents |
| Median depth (thin side) | 800 contracts |
| Median persistence | 421 seconds |
| Arbs persisting 60+ seconds | 70.7% |
| Best series | KXBTC |
| Series producing zero arbs | None |
