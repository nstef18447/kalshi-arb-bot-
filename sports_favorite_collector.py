"""Sports favorite-buy paper-trade collector.

Records observations of YES asks >= MIN_FAVORITE_PRICE on sports markets, with
top-of-book depth and the market close_time. Treats each observation as a
hypothetical TAKER buy at the observed yes_ask, applying Kalshi's general
taker fee schedule: round_up(0.07 * P * (1-P)) per contract.

Stored in maker_paper_orders with signal_type='sports_favorite_buy_taker' and
status='filled'. Resolution polling later sets resolved_price / pnl_cents using
the BUY-direction P&L formula (resolved_price - fill_price - taker_fee).
"""

import argparse
import logging
import math
import sys
import time
from datetime import datetime

import requests
from dotenv import load_dotenv

load_dotenv()

import db
import db_logger
import kalshi_api
from auth import authenticated_request

logger = logging.getLogger("sports_collector")

# --- Filters ---
MIN_FAVORITE_PRICE = 95   # cents
MIN_DEPTH = 5             # contracts available at top of YES ask
COOLDOWN_SECONDS = 3600   # one observation per (bucket, hour)
MAX_HOURS_TO_CLOSE = 48   # only watch events settling in this window (95-99c asks
                          # appear near settlement, not weeks out — and Kalshi's
                          # /events endpoint returns ~2800+ open sports events
                          # most of which are far-future and irrelevant)
MAX_EVENTS_PER_DISCOVERY = 400  # safety cap if close_time filter still leaves too many

# --- Loop tuning ---
# Each scan iterates ~650 sports series and ~20K markets. Empirically takes ~7 min
# per cycle. Sports markets don't move at crypto-15m speeds so hourly is plenty.
DISCOVERY_REFRESH_SECONDS = 1800   # (deprecated, kept for compat)
SCAN_INTERVAL_SECONDS = 1800       # 30 min between cycles (cycle itself takes ~7 min)
RESOLUTION_INTERVAL_SECONDS = 600  # check resolutions every 10 min

SIGNAL_TYPE_TAKER = "sports_favorite_buy_taker"

SPORTS_KEYWORDS = [
    "nba", "nfl", "nhl", "mlb", "ncaa", "soccer", "tennis", "mma", "ufc",
    "boxing", "golf", "pga", "wnba", "mls", "premier league", "champions league",
    "football", "basketball", "hockey", "baseball",
]

# Substrings that identify a sports market by its ticker. Kept broad — false
# positives are filtered out at price/depth check anyway, false negatives lose data.
SPORTS_TICKER_TOKENS = [
    "SPORTS", "NBA", "NFL", "NHL", "MLB", "NCAA", "EPL", "MLS", "UFC", "MMA",
    "BOX", "TENNIS", "ATP", "WTA", "GOLF", "PGA", "LPGA", "F1", "NASCAR", "WNBA",
    "STANLEY", "SUPERBOWL", "CHAMPION", "MASTERS", "CRICKET", "RUGBY",
]

# Money-line filter: only watch series that represent "who wins the game/match/fight"
# Not props, not player over/unders, not totals, not spreads.
MONEY_LINE_INCLUDES = ("GAME", "MATCH", "FIGHT", "WINNER", "BOUT")
MONEY_LINE_EXCLUDES = (
    "TOTAL", "SPREAD", "PLAYER", "PLYR", "PROP", "OTM", "OTU", "OVER", "UNDER",
    "PARLAY", "FIRST", "LAST", "SCORE", "INNING", "QUARTER", "HALF", "MARGIN",
    "RUNS", "POINTS", "GOAL", "ASSIST", "REBOUND", "STRIKEOUT", "HOMER", "HR",
    "SIX", "BOUNDARY", "WICKET", "BIRDIE", "EAGLE", "ACE", "DOUBLE",
)


def is_sports_ticker(ticker: str) -> bool:
    if not ticker:
        return False
    upper = ticker.upper()
    return any(tok in upper for tok in SPORTS_TICKER_TOKENS)


def is_money_line_series(series_ticker: str) -> bool:
    """Return True if a series_ticker looks like a 'who wins' market.

    Strategy: reject anything containing prop/over-under/spread tokens, then
    accept if it contains a game/match/fight token. Default reject — for an
    ambiguous case (championship futures, season awards) we'd rather miss data
    than record props that aren't real money-line favorites.
    """
    if not series_ticker:
        return False
    upper = series_ticker.upper()
    if any(tok in upper for tok in MONEY_LINE_EXCLUDES):
        return False
    return any(tok in upper for tok in MONEY_LINE_INCLUDES)


def _to_float(v) -> float:
    """Coerce a JSON field to float — Kalshi returns numbers as strings sometimes."""
    if v is None:
        return 0.0
    try:
        return float(v)
    except (ValueError, TypeError):
        return 0.0


def _iter_paginated_with_backoff(path: str, params: dict, items_key: str,
                                   page_sleep: float = 0.25, max_retries: int = 6):
    """Yield items page-by-page with backoff on 429s. Streaming form — caller can
    filter without accumulating the full result set in memory.

    /markets?status=open returns ~100K+ markets across all categories. Bulk-loading
    that list consumes >1GB of RAM. Streaming + early filtering keeps memory flat.
    """
    cursor = None
    page = 0
    while True:
        p = dict(params)
        if cursor:
            p["cursor"] = cursor
        backoff = 2.0
        data = None
        for attempt in range(max_retries):
            try:
                data = authenticated_request("GET", path, params=p)
                break
            except requests.exceptions.HTTPError as e:
                status = getattr(e.response, "status_code", None)
                if status == 429 and attempt < max_retries - 1:
                    logger.warning("429 on %s page %d — sleeping %.1fs (attempt %d/%d)",
                                   path, page, backoff, attempt + 1, max_retries)
                    time.sleep(backoff)
                    backoff = min(backoff * 2, 60.0)
                    continue
                raise
        if data is None:
            break
        items = data.get(items_key, [])
        page += 1
        if page % 25 == 0:
            logger.info("paginated %s: page %d, %d items this page", path, page, len(items))
        for item in items:
            yield item
        cursor = data.get("cursor")
        if not cursor or not items:
            break
        time.sleep(page_sleep)


def kalshi_taker_fee_cents(price_cents: int, contracts: int = 1) -> int:
    """General Kalshi trading fee: round_up(0.07 * C * P * (1-P)) in cents.

    P = price/100 (e.g., 0.98 for 98c). Result is rounded UP to the next cent.
    At 95-99c this returns 1c per contract; at 50c it returns 2c.
    """
    p = price_cents / 100.0
    return math.ceil(0.07 * contracts * p * (1.0 - p) * 100)


def ensure_close_time_column() -> None:
    """One-time migration: add close_time_at_signal column if missing."""
    conn = db.get_connection()
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(maker_paper_orders)").fetchall()]
        if "close_time_at_signal" not in cols:
            conn.execute("ALTER TABLE maker_paper_orders ADD COLUMN close_time_at_signal REAL")
            conn.commit()
            logger.info("Migration: added close_time_at_signal column")
    finally:
        conn.close()


def is_sports_event(ev: dict) -> bool:
    cat = (ev.get("category", "") or "").lower()
    if cat == "sports":
        return True
    text = " ".join(
        str(x) for x in [ev.get("title", ""), ev.get("series_ticker", ""), ev.get("sub_title", "")]
        if x
    ).lower()
    return any(kw in text for kw in SPORTS_KEYWORDS)


def parse_close_time(close_time_str: str) -> float | None:
    if not close_time_str:
        return None
    try:
        return datetime.fromisoformat(close_time_str.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def discover_sports_series() -> list[str]:
    """Find all open Kalshi series whose category is Sports.

    Single paginated /events?status=open call. Events carry category and
    series_ticker; we group by series and return the unique sports series list.
    Markets are then queried one series at a time, which is MUCH cheaper than
    paginating /markets?status=open (Kalshi has ~150K+ open markets total).
    """
    series: dict[str, str] = {}
    n_events = 0
    n_sports_series_seen = 0
    for ev in _iter_paginated_with_backoff(
        "/trade-api/v2/events",
        {"status": "open", "limit": 200},
        "events",
    ):
        n_events += 1
        cat = (ev.get("category", "") or "").lower()
        st = ev.get("series_ticker", "") or ""
        if not st:
            continue
        if cat == "sports":
            n_sports_series_seen += 1
            if is_money_line_series(st):
                series.setdefault(st, ev.get("title", ""))
    logger.info("Found %d money-line sports series (out of %d sports events seen) "
                "across %d total open events",
                len(series), n_sports_series_seen, n_events)
    return sorted(series.keys())


def fetch_sports_market_candidates(
    min_price: int, min_depth: int, max_hours_to_close: int,
) -> list[dict]:
    """Discover sports series, then per-series fetch markets and filter.

    Filter criteria:
    - close_time within max_hours_to_close
    - yes_ask_dollars in [min_price/100, 0.99]
    - yes_ask_size_fp >= min_depth
    """
    now = time.time()
    cutoff = now + max_hours_to_close * 3600
    min_ask = min_price / 100.0

    sports_series = discover_sports_series()
    candidates: list[dict] = []
    n_seen = 0
    for st in sports_series:
        for m in _iter_paginated_with_backoff(
            "/trade-api/v2/markets",
            {"series_ticker": st, "status": "open", "limit": 200},
            "markets",
            page_sleep=0.15,
        ):
            n_seen += 1
            yes_ask = _to_float(m.get("yes_ask_dollars"))
            if yes_ask < min_ask or yes_ask > 0.99:
                continue
            depth = _to_float(m.get("yes_ask_size_fp"))
            if depth < min_depth:
                continue
            ct = parse_close_time(m.get("close_time", "") or "")
            if ct is None or ct < now or ct > cutoff:
                continue
            candidates.append({
                "ticker": m.get("ticker", "") or "",
                "event_ticker": m.get("event_ticker", ""),
                "series_ticker": st,
                "title": m.get("title", ""),
                "yes_sub_title": m.get("yes_sub_title", ""),
                "subtitle": m.get("subtitle", ""),
                "close_time": m.get("close_time", ""),
                "_close_ts": ct,
                "yes_ask_dollars": yes_ask,
                "yes_bid_dollars": _to_float(m.get("yes_bid_dollars")),
                "yes_ask_size_fp": depth,
            })
    logger.info("Scanned %d sports markets across %d series, kept %d candidates "
                "(price>=%dc depth>=%d close<=%dh)",
                n_seen, len(sports_series), len(candidates),
                min_price, min_depth, max_hours_to_close)
    return candidates


def has_recent_observation(bucket_ticker: str, cooldown_seconds: int) -> bool:
    conn = db.get_connection(readonly=True)
    try:
        cutoff = time.time() - cooldown_seconds
        row = conn.execute(
            "SELECT 1 FROM maker_paper_orders "
            "WHERE bucket_ticker = ? AND signal_type LIKE 'sports_favorite_buy%' "
            "AND timestamp >= ? LIMIT 1",
            (bucket_ticker, cutoff),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def record_observation(
    market: dict, yes_ask: int, ask_depth: int, yes_bid: int, spread: int,
) -> None:
    """Insert a sports-favorite observation row (treated as a hypothetical taker buy)."""
    bucket_ticker = market.get("ticker", "")
    event_ticker = market.get("event_ticker", "") or bucket_ticker.rsplit("-", 1)[0]
    series_ticker = market.get("series_ticker") or bucket_ticker.split("-")[0]
    title = market.get("_event_title") or market.get("title", "")
    bucket_label = (
        market.get("yes_sub_title") or market.get("subtitle") or market.get("title", "")
    )
    close_ts = market.get("_close_ts") or parse_close_time(market.get("close_time", ""))

    now = time.time()
    conn = db.get_connection()
    try:
        conn.execute(
            "INSERT INTO maker_paper_orders "
            "(timestamp, event_ticker, series_ticker, event_title, "
            "bucket_ticker, bucket_label, category, signal_type, filter_version, "
            "limit_price, fair_value_est, "
            "yes_bid_at_signal, yes_ask_at_signal, spread_at_signal, "
            "overpricing_gap, total_event_excess, bid_depth_at_signal, "
            "status, filled_at, fill_price, fill_latency_seconds, close_time_at_signal) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (now, event_ticker, series_ticker, title,
             bucket_ticker, bucket_label, "sports", SIGNAL_TYPE_TAKER, "v1",
             yes_ask, yes_ask,
             yes_bid, yes_ask, spread,
             0, 0, ask_depth,
             "filled", now, yes_ask, 0.0, close_ts),
        )
        conn.commit()
    finally:
        conn.close()
    ttc = "?"
    if close_ts is not None:
        delta_h = (close_ts - now) / 3600.0
        ttc = f"{delta_h:.1f}h"
    logger.warning("OBSERVED %s @ %dc | depth=%d | bid=%dc spread=%dc | t-to-close=%s",
                   bucket_ticker, yes_ask, ask_depth, yes_bid, spread, ttc)


def scan_and_record(
    min_price: int, min_depth: int, cooldown: int, max_hours_to_close: int,
) -> tuple[int, int]:
    """One full cycle: bulk-fetch markets, filter, record. Returns (n_observed, n_candidates)."""
    candidates = fetch_sports_market_candidates(min_price, min_depth, max_hours_to_close)
    n_observed = 0
    for m in candidates:
        ticker = m.get("ticker", "")
        if not ticker:
            continue
        if has_recent_observation(ticker, cooldown):
            continue
        yes_ask_d = m.get("yes_ask_dollars") or 0.0
        yes_bid_d = m.get("yes_bid_dollars") or 0.0
        ask_depth = int(m.get("yes_ask_size_fp") or 0)
        yes_ask = int(round(yes_ask_d * 100))
        yes_bid = int(round(yes_bid_d * 100))
        spread = yes_ask - yes_bid
        try:
            record_observation(m, yes_ask, ask_depth, yes_bid, spread)
            n_observed += 1
        except Exception:
            logger.exception("record_observation failed for %s", ticker)
    return n_observed, len(candidates)


def resolve_open_observations() -> int:
    """Look up settlement for open sports observations and write BUY-side P&L."""
    conn = db.get_connection(readonly=True)
    try:
        rows = conn.execute(
            "SELECT id, bucket_ticker, fill_price "
            "FROM maker_paper_orders "
            "WHERE signal_type LIKE 'sports_favorite_buy%' "
            "AND status = 'filled' AND resolved_at IS NULL"
        ).fetchall()
        open_rows = [dict(r) for r in rows]
    finally:
        conn.close()

    if not open_rows:
        return 0
    logger.info("Checking %d open sports observations for resolution", len(open_rows))

    n_resolved = 0
    for r in open_rows:
        try:
            market = kalshi_api.get_market(r["bucket_ticker"])
        except Exception:
            continue
        if market.get("status") not in ("settled", "finalized"):
            continue
        result = (market.get("result", "") or "").lower()
        if result not in ("yes", "no"):
            continue
        resolved_price = 100 if result == "yes" else 0
        fill = r["fill_price"]
        fee = kalshi_taker_fee_cents(fill)
        pnl = resolved_price - fill - fee

        conn = db.get_connection()
        try:
            conn.execute(
                "UPDATE maker_paper_orders SET resolved_at = ?, resolved_price = ?, pnl_cents = ? "
                "WHERE id = ?",
                (time.time(), resolved_price, pnl, r["id"]),
            )
            conn.commit()
        finally:
            conn.close()
        n_resolved += 1
        logger.warning("RESOLVED %s | bought@%dc -> %s | fee=%dc P&L=%+dc",
                       r["bucket_ticker"], fill, result.upper(), fee, pnl)
        time.sleep(0.05)
    return n_resolved


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
        stream=sys.stdout,
    )


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--once", action="store_true",
                    help="Run one discovery + scan + resolve, then exit")
    ap.add_argument("--resolve-only", action="store_true",
                    help="Only check resolution of open observations, then exit")
    ap.add_argument("--min-price", type=int, default=MIN_FAVORITE_PRICE)
    ap.add_argument("--min-depth", type=int, default=MIN_DEPTH)
    ap.add_argument("--cooldown", type=int, default=COOLDOWN_SECONDS)
    ap.add_argument("--scan-interval", type=int, default=SCAN_INTERVAL_SECONDS)
    ap.add_argument("--discovery-refresh", type=int, default=DISCOVERY_REFRESH_SECONDS,
                    help="(deprecated, kept for compat — scan now always refreshes)")
    ap.add_argument("--resolution-interval", type=int, default=RESOLUTION_INTERVAL_SECONDS)
    ap.add_argument("--max-hours-to-close", type=int, default=MAX_HOURS_TO_CLOSE,
                    help="Only consider markets settling within this many hours")
    args = ap.parse_args()

    setup_logging()
    db_logger.init_db()
    ensure_close_time_column()

    if args.resolve_only:
        n = resolve_open_observations()
        logger.info("resolve-only complete: %d resolved", n)
        return

    if args.once:
        n_obs, n_cand = scan_and_record(
            args.min_price, args.min_depth, args.cooldown, args.max_hours_to_close,
        )
        n_res = resolve_open_observations()
        logger.info("once complete: candidates=%d observed=%d resolved=%d",
                    n_cand, n_obs, n_res)
        return

    last_resolution = 0.0
    logger.info("Sports collector running | min_price=%dc min_depth=%d cooldown=%ds "
                "max_h=%dh scan_every=%ds",
                args.min_price, args.min_depth, args.cooldown,
                args.max_hours_to_close, args.scan_interval)
    while True:
        try:
            n_obs, n_cand = scan_and_record(
                args.min_price, args.min_depth, args.cooldown, args.max_hours_to_close,
            )
            if n_obs or n_cand:
                logger.info("scan: candidates=%d observed=%d", n_cand, n_obs)

            now = time.time()
            if now - last_resolution > args.resolution_interval:
                resolve_open_observations()
                last_resolution = now
        except KeyboardInterrupt:
            logger.info("Shutdown requested")
            break
        except Exception:
            logger.exception("Scan loop error — continuing after sleep")
        time.sleep(args.scan_interval)


if __name__ == "__main__":
    main()
