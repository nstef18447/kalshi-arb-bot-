"""MLB market-snapshot research collector.

Watches every open KXMLBGAME event on Kalshi and snapshots both sides'
yes_ask / yes_bid / depth alongside the MLB game state (inning, half,
score) every N minutes. Once each game settles, records the winner and
final scores. Result: a research dataset for studying how favorites
behave throughout a baseball game (e.g. does a 60% favorite in the 5th
inning convert into a 75% favorite by the 7th when leading by 1 run?).

Schema is its own table — `mlb_market_snapshots` — separate from the
paper-trade tables so we don't conflate "hypothetical trade taken" with
"general game-state research."

Data sources:
- Kalshi REST: /events, /markets, /markets/{ticker} for KXMLBGAME
- MLB Stats API (statsapi.mlb.com): schedule + live game feed for inning + score
"""

import argparse
import logging
import re
import sys
import time
from datetime import datetime, timezone, date, timedelta
from urllib.parse import urlencode

import requests
from dotenv import load_dotenv

load_dotenv()

import db
import db_logger
import kalshi_api

logger = logging.getLogger("mlb_snapshot")

# --- Loop tuning ---
SNAPSHOT_INTERVAL_SECONDS = 300        # one snapshot per game per 5 min
DISCOVERY_REFRESH_SECONDS = 600        # refresh Kalshi event list every 10 min
SCHEDULE_REFRESH_SECONDS = 1800        # refresh MLB schedule every 30 min
RESOLUTION_INTERVAL_SECONDS = 600      # check resolutions every 10 min

KALSHI_SERIES = "KXMLBGAME"
MLB_API = "https://statsapi.mlb.com/api/v1"
MLB_API_LIVE = "https://statsapi.mlb.com/api/v1.1"  # live feed uses v1.1

# Active MLB game states (skip warm-up scheduled / postponed / final)
LIVE_STATES = {"In Progress", "Manager Challenge", "Delayed", "Warmup"}
DONE_STATES = {"Final", "Game Over", "Completed Early"}


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS mlb_market_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    -- Identity
    kalshi_event_ticker TEXT NOT NULL,
    away_team TEXT NOT NULL,
    home_team TEXT NOT NULL,
    game_date TEXT NOT NULL,
    external_game_id TEXT,
    -- Game state (null if not matched to MLB API)
    game_status TEXT,
    inning INTEGER,
    inning_half TEXT,
    away_score INTEGER,
    home_score INTEGER,
    -- Kalshi market state (both sides)
    away_ticker TEXT,
    home_ticker TEXT,
    away_yes_ask INTEGER,
    away_yes_bid INTEGER,
    away_depth INTEGER,
    home_yes_ask INTEGER,
    home_yes_bid INTEGER,
    home_depth INTEGER,
    -- Resolution (filled in after settlement)
    resolved_at REAL,
    resolved_winner TEXT,
    final_away_score INTEGER,
    final_home_score INTEGER
);
CREATE INDEX IF NOT EXISTS idx_mlb_snap_event ON mlb_market_snapshots(kalshi_event_ticker);
CREATE INDEX IF NOT EXISTS idx_mlb_snap_game ON mlb_market_snapshots(external_game_id);
CREATE INDEX IF NOT EXISTS idx_mlb_snap_ts ON mlb_market_snapshots(timestamp);
CREATE INDEX IF NOT EXISTS idx_mlb_snap_unresolved ON mlb_market_snapshots(resolved_at) WHERE resolved_at IS NULL;
"""


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
        stream=sys.stdout,
    )


def init_schema() -> None:
    conn = db.get_connection()
    try:
        conn.executescript(SCHEMA_SQL)
        conn.commit()
    finally:
        conn.close()


def get_window_dates() -> list[str]:
    today = date.today()
    return [today.isoformat(), (today + timedelta(days=1)).isoformat()]


def fetch_mlb_schedule(date_str: str) -> list[dict]:
    url = f"{MLB_API}/schedule?{urlencode({'sportId': 1, 'date': date_str, 'hydrate': 'team,linescore'})}"
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    games: list[dict] = []
    for d in r.json().get("dates", []):
        games.extend(d.get("games", []))
    return games


def fetch_mlb_game_live(game_pk: int) -> dict:
    url = f"{MLB_API_LIVE}/game/{game_pk}/feed/live"
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    return r.json()


def match_kalshi_to_mlb(kalshi_event_title: str, mlb_games: list[dict]) -> dict | None:
    title = (kalshi_event_title or "").lower()
    parts = re.split(r"\s+vs\.?\s+|\s+@\s+", title)
    if len(parts) != 2:
        return None
    a_kalshi, b_kalshi = parts[0].strip(), parts[1].strip()
    for g in mlb_games:
        away_name = (g.get("teams", {}).get("away", {}).get("team", {}).get("name") or "").lower()
        home_name = (g.get("teams", {}).get("home", {}).get("team", {}).get("name") or "").lower()
        if (a_kalshi in away_name and b_kalshi in home_name) or \
           (a_kalshi in home_name and b_kalshi in away_name):
            return g
    return None


def discover_kalshi_mlb_events() -> list[dict]:
    events = kalshi_api.get_events(KALSHI_SERIES, status="open")
    return events


def fetch_event_markets(event_ticker: str) -> list[dict]:
    return kalshi_api.get_markets_for_event(event_ticker, status="open")


def has_recent_snapshot(event_ticker: str, cooldown_seconds: int) -> bool:
    conn = db.get_connection(readonly=True)
    try:
        cutoff = time.time() - cooldown_seconds
        row = conn.execute(
            "SELECT 1 FROM mlb_market_snapshots "
            "WHERE kalshi_event_ticker = ? AND timestamp >= ? LIMIT 1",
            (event_ticker, cutoff),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def split_markets_by_team(markets: list[dict], away_name: str, home_name: str) -> tuple[dict | None, dict | None]:
    """Identify which market is the away vs home YES contract.

    Match by yes_sub_title (e.g. 'Atlanta', 'Seattle') against the team name.
    """
    away_m = home_m = None
    aw, hm = (away_name or "").lower(), (home_name or "").lower()
    for m in markets:
        sub = (m.get("yes_sub_title") or "").lower()
        if sub and sub in aw:
            away_m = m
        elif sub and sub in hm:
            home_m = m
    return away_m, home_m


def to_cents(v) -> int:
    try:
        return int(round(float(v or 0) * 100))
    except (ValueError, TypeError):
        return 0


def to_int(v) -> int:
    try:
        return int(float(v or 0))
    except (ValueError, TypeError):
        return 0


def snapshot_event(event: dict, mlb_games: list[dict]) -> bool:
    """Take one snapshot of a Kalshi event + matched MLB game state. Returns True if written."""
    et = event.get("event_ticker", "") or ""
    title = event.get("title", "") or ""
    if not et:
        return False

    mlb_game = match_kalshi_to_mlb(title, mlb_games)
    away_name = (mlb_game.get("teams", {}).get("away", {}).get("team", {}).get("name") or "?") if mlb_game else "?"
    home_name = (mlb_game.get("teams", {}).get("home", {}).get("team", {}).get("name") or "?") if mlb_game else "?"
    game_date = (mlb_game.get("officialDate") or mlb_game.get("gameDate", "")[:10]) if mlb_game else ""
    game_pk = mlb_game.get("gamePk") if mlb_game else None
    state = (mlb_game.get("status", {}).get("detailedState") if mlb_game else None) or ""

    inning = inning_half = None
    away_score = home_score = None

    # Pull live state for in-progress games
    if mlb_game and state in LIVE_STATES and game_pk:
        try:
            live = fetch_mlb_game_live(int(game_pk))
            ls = live.get("liveData", {}).get("linescore", {})
            inning = ls.get("currentInning")
            inning_half = ls.get("inningHalf")
            away_score = ls.get("teams", {}).get("away", {}).get("runs")
            home_score = ls.get("teams", {}).get("home", {}).get("runs")
        except Exception:
            logger.debug("MLB live fetch failed for %s gamePk=%s", et, game_pk)

    # Pull current Kalshi market state for both sides
    try:
        markets = fetch_event_markets(et)
    except Exception:
        return False

    away_m, home_m = split_markets_by_team(markets, away_name, home_name)
    # Fallback: if matching by name failed, just take first 2 markets
    if (away_m is None or home_m is None) and len(markets) == 2:
        away_m, home_m = markets[0], markets[1]
    if away_m is None and home_m is None:
        return False

    # Construct row
    now = time.time()
    row = (
        now, et, away_name, home_name, game_date or "", str(game_pk) if game_pk else None,
        state or None, inning, inning_half, away_score, home_score,
        away_m.get("ticker") if away_m else None,
        home_m.get("ticker") if home_m else None,
        to_cents(away_m.get("yes_ask_dollars")) if away_m else None,
        to_cents(away_m.get("yes_bid_dollars")) if away_m else None,
        to_int(away_m.get("yes_ask_size_fp")) if away_m else None,
        to_cents(home_m.get("yes_ask_dollars")) if home_m else None,
        to_cents(home_m.get("yes_bid_dollars")) if home_m else None,
        to_int(home_m.get("yes_ask_size_fp")) if home_m else None,
    )
    conn = db.get_connection()
    try:
        conn.execute(
            "INSERT INTO mlb_market_snapshots "
            "(timestamp, kalshi_event_ticker, away_team, home_team, game_date, external_game_id, "
            "game_status, inning, inning_half, away_score, home_score, "
            "away_ticker, home_ticker, "
            "away_yes_ask, away_yes_bid, away_depth, "
            "home_yes_ask, home_yes_bid, home_depth) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            row,
        )
        conn.commit()
    finally:
        conn.close()
    return True


def resolve_settled_games() -> int:
    """For events whose snapshots have no resolution yet, check if the Kalshi market settled
    and write the winner + final scores into all that event's rows."""
    conn = db.get_connection(readonly=True)
    try:
        rows = conn.execute(
            "SELECT DISTINCT kalshi_event_ticker, away_ticker, home_ticker, external_game_id "
            "FROM mlb_market_snapshots WHERE resolved_at IS NULL"
        ).fetchall()
        unresolved = [dict(r) for r in rows]
    finally:
        conn.close()

    if not unresolved:
        return 0

    n_resolved_events = 0
    for u in unresolved:
        away_t = u["away_ticker"]
        home_t = u["home_ticker"]
        if not away_t and not home_t:
            continue
        # Check whichever ticker we have first
        sample = away_t or home_t
        try:
            mkt = kalshi_api.get_market(sample)
        except Exception:
            continue
        if mkt.get("status") not in ("settled", "finalized"):
            continue
        # Determine winner from each side's result
        away_result = home_result = None
        try:
            if away_t:
                am = kalshi_api.get_market(away_t)
                away_result = (am.get("result") or "").lower()
            if home_t:
                hm = kalshi_api.get_market(home_t)
                home_result = (hm.get("result") or "").lower()
        except Exception:
            pass
        winner = None
        if away_result == "yes" or home_result == "no":
            winner = "away"
        elif home_result == "yes" or away_result == "no":
            winner = "home"
        if winner is None:
            continue

        final_away = final_home = None
        if u.get("external_game_id"):
            try:
                live = fetch_mlb_game_live(int(u["external_game_id"]))
                ls = live.get("liveData", {}).get("linescore", {})
                final_away = ls.get("teams", {}).get("away", {}).get("runs")
                final_home = ls.get("teams", {}).get("home", {}).get("runs")
            except Exception:
                pass

        conn = db.get_connection()
        try:
            conn.execute(
                "UPDATE mlb_market_snapshots SET resolved_at = ?, resolved_winner = ?, "
                "final_away_score = ?, final_home_score = ? "
                "WHERE kalshi_event_ticker = ? AND resolved_at IS NULL",
                (time.time(), winner, final_away, final_home, u["kalshi_event_ticker"]),
            )
            conn.commit()
        finally:
            conn.close()
        n_resolved_events += 1
        logger.warning("RESOLVED %s -> %s wins (final %s-%s)",
                       u["kalshi_event_ticker"], winner.upper(),
                       final_away, final_home)
    return n_resolved_events


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--resolve-only", action="store_true")
    ap.add_argument("--snapshot-interval", type=int, default=SNAPSHOT_INTERVAL_SECONDS)
    ap.add_argument("--discovery-refresh", type=int, default=DISCOVERY_REFRESH_SECONDS)
    ap.add_argument("--schedule-refresh", type=int, default=SCHEDULE_REFRESH_SECONDS)
    ap.add_argument("--resolution-interval", type=int, default=RESOLUTION_INTERVAL_SECONDS)
    args = ap.parse_args()

    setup_logging()
    db_logger.init_db()
    init_schema()

    if args.resolve_only:
        n = resolve_settled_games()
        logger.info("resolve-only: %d events resolved", n)
        return

    kalshi_events: list[dict] = []
    mlb_games: list[dict] = []
    last_discovery = 0.0
    last_schedule = 0.0
    last_resolution = 0.0

    def refresh_kalshi():
        nonlocal kalshi_events
        try:
            kalshi_events = discover_kalshi_mlb_events()
            logger.info("Kalshi: %d open KXMLBGAME events", len(kalshi_events))
        except Exception:
            logger.exception("Kalshi discovery failed")

    def refresh_mlb():
        nonlocal mlb_games
        try:
            mlb_games = []
            for d in get_window_dates():
                mlb_games.extend(fetch_mlb_schedule(d))
            n_live = sum(1 for g in mlb_games if g.get("status", {}).get("detailedState") in LIVE_STATES)
            logger.info("MLB: %d games (%d in progress)", len(mlb_games), n_live)
        except Exception:
            logger.exception("MLB schedule fetch failed")

    def cycle():
        # Cooldown is just under snapshot-interval so we don't double-record
        cooldown = max(args.snapshot_interval - 30, 60)
        n_written = 0
        for ev in kalshi_events:
            et = ev.get("event_ticker", "") or ""
            if not et:
                continue
            if has_recent_snapshot(et, cooldown):
                continue
            try:
                if snapshot_event(ev, mlb_games):
                    n_written += 1
            except Exception:
                logger.exception("snapshot_event failed for %s", et)
        return n_written

    refresh_kalshi(); last_discovery = time.time()
    refresh_mlb();    last_schedule = time.time()

    if args.once:
        n = cycle()
        m = resolve_settled_games()
        logger.info("once complete: events=%d snapshots_written=%d resolved=%d",
                    len(kalshi_events), n, m)
        return

    logger.info("MLB snapshot collector running | snapshot_every=%ds events=%d",
                args.snapshot_interval, len(kalshi_events))
    while True:
        try:
            now = time.time()
            if now - last_discovery > args.discovery_refresh:
                refresh_kalshi(); last_discovery = now
            if now - last_schedule > args.schedule_refresh:
                refresh_mlb(); last_schedule = now

            n = cycle()
            if n:
                logger.info("cycle: events=%d snapshots_written=%d", len(kalshi_events), n)

            if now - last_resolution > args.resolution_interval:
                resolve_settled_games(); last_resolution = now
        except KeyboardInterrupt:
            logger.info("Shutdown")
            break
        except Exception:
            logger.exception("loop error — continuing")
        time.sleep(min(args.snapshot_interval, 60))


if __name__ == "__main__":
    main()
