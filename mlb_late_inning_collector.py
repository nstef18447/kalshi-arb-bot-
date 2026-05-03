"""MLB late-inning favorite-buy paper-trade collector.

Strategy: when an MLB game is past the 6th inning (currentInning >= 7) AND a
team's YES is trading at >= 70c on Kalshi (KXMLBGAME), record an observation
as a hypothetical TAKER buy at the observed yes_ask. Resolve via Kalshi
market.result on settlement.

Data sources:
- Kalshi REST: /events and /markets for KXMLBGAME series
- MLB Stats API: statsapi.mlb.com/api/v1/schedule + .../game/{pk}/feed/live for inning state

Stored in maker_paper_orders with signal_type='mlb_late_inning_favorite' and
status='filled'.
"""

import argparse
import logging
import math
import re
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urlencode

import requests
from dotenv import load_dotenv

load_dotenv()

import db
import db_logger
import kalshi_api

logger = logging.getLogger("mlb_late_inning")

# --- Strategy filters ---
MIN_INNING = 7              # past the 6th inning means 7th or later
MIN_FAVORITE_PRICE = 70     # cents (0.70)
MAX_FAVORITE_PRICE = 99     # don't waste capital at 99c+
MIN_DEPTH = 5               # contracts at top of yes ask
COOLDOWN_SECONDS = 86400    # one observation per (bucket_ticker) per day; same
                            # game won't be re-recorded once captured

# --- Loop tuning ---
POLL_INTERVAL_SECONDS = 30          # check all tracked games every 30s
DISCOVERY_REFRESH_SECONDS = 600     # refresh Kalshi events list every 10 min
RESOLUTION_INTERVAL_SECONDS = 300   # check resolutions every 5 min

SIGNAL_TYPE = "mlb_late_inning_favorite"
KALSHI_SERIES = "KXMLBGAME"
MLB_API = "https://statsapi.mlb.com/api/v1"

# Ticker format: KXMLBGAME-YYMMMDDhhmm{AWAY}{HOME}-{TEAM}
# e.g.  KXMLBGAME-26MAY061610ATLSEA-SEA
TICKER_RE = re.compile(
    r"^KXMLBGAME-(\d{2})([A-Z]{3})(\d{2})(\d{4})([A-Z]{2,4})-([A-Z]{2,4})$"
)


def kalshi_taker_fee_cents(price_cents: int, contracts: int = 1) -> int:
    """General Kalshi trading fee: round_up(0.07 * C * P * (1-P)) in cents."""
    p = price_cents / 100.0
    return math.ceil(0.07 * contracts * p * (1.0 - p) * 100)


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
        stream=sys.stdout,
    )


def ensure_extra_columns() -> None:
    """Migrations: add columns we need beyond the original schema."""
    needed = {
        "close_time_at_signal": "REAL",
        "current_inning_at_signal": "INTEGER",
        "external_game_id": "TEXT",
    }
    conn = db.get_connection()
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(maker_paper_orders)").fetchall()}
        for name, dtype in needed.items():
            if name not in cols:
                conn.execute(f"ALTER TABLE maker_paper_orders ADD COLUMN {name} {dtype}")
                logger.info("Migration: added %s column", name)
        conn.commit()
    finally:
        conn.close()


def parse_kalshi_event_ticker(event_ticker: str) -> dict | None:
    """Parse KXMLBGAME-26MAY061610ATLSEA -> {date, time, away_code, home_code}.

    Note: this is the EVENT ticker (no trailing -TEAM). For markets we suffix.
    """
    # Kalshi event tickers look like KXMLBGAME-26MAY061610ATLSEA  (no trailing -TEAM)
    m = re.match(r"^KXMLBGAME-(\d{2})([A-Z]{3})(\d{2})(\d{4})([A-Z]{2,4})$", event_ticker)
    if not m:
        return None
    yy, mon_abbr, dd, hhmm, teams_blob = m.groups()
    months = {"JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,
              "JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12}
    if mon_abbr not in months:
        return None
    year = 2000 + int(yy)
    # Greedy 3-char split for teams blob (most common: AAABBB).
    if len(teams_blob) == 6:
        away, home = teams_blob[:3], teams_blob[3:]
    elif len(teams_blob) == 7:
        # ambiguous; try 3+4 first (LAA, OAK common 3-char codes)
        away, home = teams_blob[:3], teams_blob[3:]
    else:
        away, home = teams_blob[:3], teams_blob[3:]
    return {
        "year": year, "month": months[mon_abbr], "day": int(dd),
        "hour": int(hhmm[:2]), "minute": int(hhmm[2:]),
        "away_code": away, "home_code": home,
    }


def fetch_mlb_schedule(date_str: str) -> list[dict]:
    """Fetch MLB games for date_str (YYYY-MM-DD). Hydrates with team metadata."""
    url = f"{MLB_API}/schedule?{urlencode({'sportId': 1, 'date': date_str, 'hydrate': 'team,linescore'})}"
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    data = r.json()
    games = []
    for d in data.get("dates", []):
        games.extend(d.get("games", []))
    return games


def fetch_mlb_game_live(game_pk: int) -> dict:
    """Fetch live feed for a game — returns linescore + status snapshot."""
    url = f"{MLB_API}/game/{game_pk}/feed/live"
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    return r.json()


def match_kalshi_to_mlb(kalshi_event_title: str, mlb_games: list[dict]) -> dict | None:
    """Match a Kalshi event by title (e.g. 'Atlanta vs Seattle') to an MLB game.

    Strategy: parse the two team names from the title and substring-match
    against the MLB game's teams.{away,home}.team.name fields.
    """
    title = (kalshi_event_title or "").lower()
    # Title format: "Atlanta vs Seattle" — extract the two halves
    parts = re.split(r"\s+vs\.?\s+|\s+@\s+", title)
    if len(parts) != 2:
        return None
    a_kalshi, b_kalshi = parts[0].strip(), parts[1].strip()

    for g in mlb_games:
        away_name = ((g.get("teams", {}).get("away", {}).get("team", {}).get("name") or "")).lower()
        home_name = ((g.get("teams", {}).get("home", {}).get("team", {}).get("name") or "")).lower()
        # Kalshi sometimes writes "Chicago WS" or "New York M" — substring match
        # Either ordering on Kalshi side; MLB always away vs home.
        a_match = a_kalshi in away_name or away_name.startswith(a_kalshi)
        b_match = b_kalshi in home_name or home_name.startswith(b_kalshi)
        if a_match and b_match:
            return g
        # Try the swap (Kalshi might list home first)
        a_match2 = a_kalshi in home_name or home_name.startswith(a_kalshi)
        b_match2 = b_kalshi in away_name or away_name.startswith(b_kalshi)
        if a_match2 and b_match2:
            return g
    return None


def discover_kalshi_mlb_events() -> list[dict]:
    """Fetch all currently-open KXMLBGAME events."""
    events = kalshi_api.get_events(KALSHI_SERIES, status="open")
    logger.info("Kalshi: %d open KXMLBGAME events", len(events))
    return events


def has_recent_observation(bucket_ticker: str, cooldown_seconds: int) -> bool:
    conn = db.get_connection(readonly=True)
    try:
        cutoff = time.time() - cooldown_seconds
        row = conn.execute(
            "SELECT 1 FROM maker_paper_orders "
            "WHERE bucket_ticker = ? AND signal_type = ? AND timestamp >= ? LIMIT 1",
            (bucket_ticker, SIGNAL_TYPE, cutoff),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def record_observation(market: dict, event_title: str, yes_ask: int, ask_depth: int,
                        yes_bid: int, spread: int, inning: int, game_pk: int) -> None:
    bucket_ticker = market.get("ticker", "")
    event_ticker = market.get("event_ticker", "")
    series_ticker = market.get("series_ticker", KALSHI_SERIES)
    bucket_label = market.get("yes_sub_title") or market.get("subtitle") or market.get("title", "")

    close_ts = None
    ct_str = market.get("close_time", "") or ""
    if ct_str:
        try:
            close_ts = datetime.fromisoformat(ct_str.replace("Z", "+00:00")).timestamp()
        except (ValueError, TypeError):
            pass

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
            "status, filled_at, fill_price, fill_latency_seconds, "
            "close_time_at_signal, current_inning_at_signal, external_game_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (now, event_ticker, series_ticker, event_title,
             bucket_ticker, bucket_label, "sports", SIGNAL_TYPE, "v1",
             yes_ask, yes_ask,
             yes_bid, yes_ask, spread,
             0, 0, ask_depth,
             "filled", now, yes_ask, 0.0,
             close_ts, inning, str(game_pk)),
        )
        conn.commit()
    finally:
        conn.close()
    logger.warning("OBSERVED %s @ %dc | inning=%d depth=%d bid=%dc | %s | gamePk=%s",
                   bucket_ticker, yes_ask, inning, ask_depth, yes_bid, event_title, game_pk)


def fetch_kalshi_event_markets(event_ticker: str) -> list[dict]:
    """Get current state of all markets for an event."""
    return kalshi_api.get_markets_for_event(event_ticker, status="open")


def scan_one_event(event: dict, mlb_games_today: list[dict]) -> int:
    """One event = one MLB game = 2 markets (one per team). Returns n observed."""
    et = event.get("event_ticker", "")
    title = event.get("title", "")
    mlb_game = match_kalshi_to_mlb(title, mlb_games_today)
    if not mlb_game:
        return 0

    state = mlb_game.get("status", {}).get("detailedState", "")
    if state in ("Final", "Game Over", "Completed Early"):
        return 0  # game already done; resolution will catch it

    # Pull live linescore for current inning
    game_pk = mlb_game.get("gamePk")
    if not game_pk:
        return 0
    try:
        live = fetch_mlb_game_live(game_pk)
    except Exception:
        return 0
    inning = (live.get("liveData", {}).get("linescore", {}).get("currentInning") or 0)
    if inning < MIN_INNING:
        return 0

    # Pull fresh Kalshi market state for this event
    try:
        markets = fetch_kalshi_event_markets(et)
    except Exception:
        return 0

    n_recorded = 0
    for m in markets:
        ticker = m.get("ticker", "")
        if not ticker:
            continue
        yes_ask_d = float(m.get("yes_ask_dollars") or 0.0)
        yes_bid_d = float(m.get("yes_bid_dollars") or 0.0)
        depth = float(m.get("yes_ask_size_fp") or 0.0)
        yes_ask = int(round(yes_ask_d * 100))
        yes_bid = int(round(yes_bid_d * 100))
        if yes_ask < MIN_FAVORITE_PRICE or yes_ask > MAX_FAVORITE_PRICE:
            continue
        if depth < MIN_DEPTH:
            continue
        if has_recent_observation(ticker, COOLDOWN_SECONDS):
            continue
        spread = yes_ask - yes_bid
        try:
            record_observation(m, title, yes_ask, int(depth), yes_bid, spread, inning, game_pk)
            n_recorded += 1
        except Exception:
            logger.exception("record_observation failed for %s", ticker)
    return n_recorded


def resolve_open_observations() -> int:
    conn = db.get_connection(readonly=True)
    try:
        rows = conn.execute(
            "SELECT id, bucket_ticker, fill_price FROM maker_paper_orders "
            "WHERE signal_type = ? AND status = 'filled' AND resolved_at IS NULL",
            (SIGNAL_TYPE,),
        ).fetchall()
        open_rows = [dict(r) for r in rows]
    finally:
        conn.close()
    if not open_rows:
        return 0
    logger.info("Checking %d open MLB observations for resolution", len(open_rows))

    n = 0
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
        n += 1
        logger.warning("RESOLVED %s | bought@%dc -> %s | fee=%dc P&L=%+dc",
                       r["bucket_ticker"], fill, result.upper(), fee, pnl)
    return n


def get_mlb_window_dates() -> list[str]:
    """Today and tomorrow (UTC) — covers in-progress games and ones starting soon."""
    from datetime import date, timedelta
    today = date.today()
    return [today.isoformat(), (today + timedelta(days=1)).isoformat()]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--once", action="store_true", help="One pass, then exit")
    ap.add_argument("--resolve-only", action="store_true")
    ap.add_argument("--poll-interval", type=int, default=POLL_INTERVAL_SECONDS)
    ap.add_argument("--discovery-refresh", type=int, default=DISCOVERY_REFRESH_SECONDS)
    ap.add_argument("--resolution-interval", type=int, default=RESOLUTION_INTERVAL_SECONDS)
    args = ap.parse_args()

    setup_logging()
    db_logger.init_db()
    ensure_extra_columns()

    if args.resolve_only:
        n = resolve_open_observations()
        logger.info("resolve-only: %d resolved", n)
        return

    last_discovery = 0.0
    last_resolution = 0.0
    last_schedule = 0.0
    kalshi_events: list[dict] = []
    mlb_games: list[dict] = []

    def refresh_discovery():
        nonlocal kalshi_events, mlb_games
        try:
            kalshi_events = discover_kalshi_mlb_events()
        except Exception:
            logger.exception("Kalshi discovery failed")
        try:
            mlb_games = []
            for d in get_mlb_window_dates():
                mlb_games.extend(fetch_mlb_schedule(d))
            in_progress = [g for g in mlb_games
                           if g.get("status", {}).get("detailedState") in
                           ("In Progress", "Manager Challenge", "Delayed", "Warmup")]
            logger.info("MLB schedule: %d games (%d in progress)", len(mlb_games), len(in_progress))
        except Exception:
            logger.exception("MLB schedule fetch failed")

    def cycle():
        n_obs = 0
        for ev in kalshi_events:
            try:
                n_obs += scan_one_event(ev, mlb_games)
            except Exception:
                logger.exception("scan_one_event failed for %s", ev.get("event_ticker"))
        return n_obs

    refresh_discovery()
    last_discovery = time.time()

    if args.once:
        n_obs = cycle()
        n_res = resolve_open_observations()
        logger.info("once complete: events=%d observed=%d resolved=%d",
                    len(kalshi_events), n_obs, n_res)
        return

    logger.info("MLB late-inning collector running | min_inning=%d min_price=%dc poll=%ds",
                MIN_INNING, MIN_FAVORITE_PRICE, args.poll_interval)
    while True:
        try:
            now = time.time()
            if now - last_discovery > args.discovery_refresh:
                refresh_discovery()
                last_discovery = now

            n_obs = cycle()
            if n_obs:
                logger.info("cycle: events=%d observed=%d", len(kalshi_events), n_obs)

            if now - last_resolution > args.resolution_interval:
                resolve_open_observations()
                last_resolution = now
        except KeyboardInterrupt:
            logger.info("Shutdown")
            break
        except Exception:
            logger.exception("loop error — continuing")
        time.sleep(args.poll_interval)


if __name__ == "__main__":
    main()
