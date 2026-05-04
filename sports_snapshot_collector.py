"""Unified sports market-snapshot research collector (MLB / NHL / NBA / NFL).

For every open Kalshi market in the four major-sport money-line series, this
script snapshots both sides' yes_ask / yes_bid / depth alongside the live
game state (period/inning/quarter, clock, score) every N minutes. Once a
game settles, records the winner and final scores into all snapshot rows.

Strategy this dataset is meant to validate: deep-favorite buys (85-95c) on
heavy favorites *after* a sport-specific high-confidence threshold (MLB
inning >= 7, NHL P3, NBA/NFL Q4, soccer 75+min, tennis final set, UFC last
round). Without intra-game price + state history we can't backtest it.

Game-state source: ESPN's free public scoreboard API
  https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/scoreboard?dates=YYYYMMDD

Match key: (sport, date) selects ESPN candidates; team displayName / name
substring against the parsed Kalshi event title picks the specific game.
Unmatched events still record price snapshots; the unresolved tickers go
to sports_match_failures so we can add aliases over time.

Schema is its own table (sports_market_snapshots) so this stays isolated
from the production paper-trade tables.
"""

import argparse
import json
import logging
import re
import sys
import time
from datetime import datetime, date, timedelta
from urllib.parse import urlencode

import requests
from dotenv import load_dotenv

load_dotenv()

import db
import db_logger
import kalshi_api

logger = logging.getLogger("sports_snapshot")

# --- Loop tuning ---
SNAPSHOT_INTERVAL_SECONDS = 300        # one snapshot per game per 5 min
DISCOVERY_REFRESH_SECONDS = 600        # refresh Kalshi event list every 10 min
SCOREBOARD_REFRESH_SECONDS = 180       # refresh ESPN scoreboards every 3 min (cheaper than Kalshi)
RESOLUTION_INTERVAL_SECONDS = 600      # check resolutions every 10 min

# Kalshi money-line series we care about. Hardcoded — these are stable, and
# enumerating them avoids a 2,000+-event /events scan per discovery cycle.
KALSHI_SERIES_TO_SPORT = {
    "KXMLBGAME": "MLB",
    "KXNHLGAME": "NHL",
    "KXNBAGAME": "NBA",
    "KXNFLGAME": "NFL",
}

ESPN_SCOREBOARD = {
    "MLB": "https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/scoreboard",
    "NHL": "https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/scoreboard",
    "NBA": "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard",
    "NFL": "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard",
}

MONTH_MAP = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}

# Kalshi event ticker pattern: KXMLBGAME-25NOV04CHCNYM, KXNHLGAME-25NOV04TBLBOS, etc.
TICKER_DATE_RE = re.compile(r"^[A-Z0-9]+-(\d{2}[A-Z]{3}\d{2})")

# Kalshi event titles use short city+initial codes for teams that share a city
# (e.g. "Chicago WS" = White Sox, "Los Angeles A" = Angels). ESPN uses full
# names. Map Kalshi shorthand -> a list of substrings any of which should match
# an ESPN team name. Lower-case throughout. Add entries here when the dry-run
# logs in-window unmatched events, and the gap shrinks each run.
TEAM_ALIASES: dict[str, dict[str, list[str]]] = {
    "MLB": {
        "a's": ["athletics", "oakland"],
        "chicago ws": ["white sox", "chw"],
        "chicago c": ["cubs", "chc"],
        "los angeles a": ["angels", "anaheim", "laa"],
        "los angeles d": ["dodgers", "lad"],
        "new york y": ["yankees", "nyy"],
        "new york m": ["mets", "nym"],
        "san francisco": ["giants", "sf"],
        "tampa bay": ["rays", "tb"],
        "kansas city": ["royals", "kc"],
    },
    "NBA": {
        "los angeles l": ["lakers", "lal"],
        "los angeles c": ["clippers", "lac"],
        "new york": ["knicks", "nyk"],
        "brooklyn": ["nets", "bkn"],
        "golden state": ["warriors", "gsw"],
        "san antonio": ["spurs", "sas"],
        "oklahoma city": ["thunder", "okc"],
    },
    "NHL": {
        "new york r": ["rangers", "nyr"],
        "new york i": ["islanders", "nyi"],
        "los angeles": ["kings", "lak"],
        "tampa bay": ["lightning", "tbl"],
        "vegas": ["golden knights", "vgk"],
        "st louis": ["blues", "stl"],
    },
    "NFL": {
        "new york g": ["giants", "nyg"],
        "new york j": ["jets", "nyj"],
        "los angeles r": ["rams", "lar"],
        "los angeles c": ["chargers", "lac"],
        "tampa bay": ["buccaneers", "tb"],
        "kansas city": ["chiefs", "kc"],
    },
}

# Strip a leading "Game N:" or "Game N -" prefix before parsing two-team titles.
# Kalshi uses this for playoff series ("Game 4: Vegas at Anaheim").
GAME_PREFIX_RE = re.compile(r"^game\s+\d+\s*[:\-]\s*", re.IGNORECASE)


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sports_market_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    sport TEXT NOT NULL,
    series_ticker TEXT NOT NULL,
    -- Identity
    kalshi_event_ticker TEXT NOT NULL,
    away_team TEXT,
    home_team TEXT,
    game_date TEXT,
    external_game_id TEXT,
    -- Game state (null if not matched to ESPN)
    game_status TEXT,
    game_status_detail TEXT,
    period INTEGER,
    period_label TEXT,
    clock TEXT,
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
CREATE INDEX IF NOT EXISTS idx_sports_snap_sport ON sports_market_snapshots(sport);
CREATE INDEX IF NOT EXISTS idx_sports_snap_event ON sports_market_snapshots(kalshi_event_ticker);
CREATE INDEX IF NOT EXISTS idx_sports_snap_ts ON sports_market_snapshots(timestamp);
CREATE INDEX IF NOT EXISTS idx_sports_snap_unresolved ON sports_market_snapshots(resolved_at) WHERE resolved_at IS NULL;

CREATE TABLE IF NOT EXISTS sports_match_failures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    sport TEXT NOT NULL,
    kalshi_event_ticker TEXT NOT NULL,
    kalshi_event_title TEXT,
    parsed_date TEXT,
    espn_candidates TEXT,
    reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_match_fail_sport ON sports_match_failures(sport);
CREATE INDEX IF NOT EXISTS idx_match_fail_ts ON sports_match_failures(timestamp);
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


# ---------- Date / ticker parsing ----------

def parse_kalshi_ticker_date(event_ticker: str) -> date | None:
    """Extract YYMMMDD prefix from a Kalshi event ticker. Returns None on fail."""
    m = TICKER_DATE_RE.search((event_ticker or "").upper())
    if not m:
        return None
    s = m.group(1)
    try:
        yy, mon, dd = int(s[0:2]), MONTH_MAP.get(s[2:5]), int(s[5:7])
        if not mon:
            return None
        return date(2000 + yy, mon, dd)
    except (ValueError, TypeError):
        return None


def get_lookup_dates() -> list[date]:
    """Days we want ESPN data for: yesterday (settlement lag) through +7 days.

    Wider than strictly needed for live snapshots (which only care about today),
    but this is what the matcher uses to identify which Kalshi event corresponds
    to which ESPN game. Playoff events on Kalshi are dated several days out, so
    we need ESPN data for those days too. ESPN scoreboard calls are cheap (~30
    HTTP calls per refresh), so the wider window costs little.
    """
    today = date.today()
    return [today + timedelta(days=off) for off in range(-1, 8)]


# ---------- ESPN scoreboard fetching ----------

def fetch_espn_scoreboard(sport: str, day: date) -> list[dict]:
    """Fetch ESPN scoreboard for one sport on one day. Returns list of events."""
    url = ESPN_SCOREBOARD.get(sport)
    if not url:
        return []
    params = {"dates": day.strftime("%Y%m%d")}
    try:
        r = requests.get(f"{url}?{urlencode(params)}", timeout=10)
        r.raise_for_status()
        return r.json().get("events", [])
    except Exception:
        logger.exception("ESPN fetch failed: sport=%s date=%s", sport, day)
        return []


def refresh_all_scoreboards() -> dict[tuple[str, str], list[dict]]:
    """Pull ESPN scoreboards for all four sports across our date window.

    Returns {(sport, 'YYYY-MM-DD'): [espn_events]} cache.
    """
    cache: dict[tuple[str, str], list[dict]] = {}
    for sport in ESPN_SCOREBOARD:
        for d in get_lookup_dates():
            evs = fetch_espn_scoreboard(sport, d)
            cache[(sport, d.isoformat())] = evs
    total = sum(len(v) for v in cache.values())
    logger.info("ESPN: %d events cached across %d (sport,date) pairs", total, len(cache))
    return cache


# ---------- Game-state extraction ----------

def _to_int_or_none(v):
    try:
        if v is None or v == "":
            return None
        return int(float(v))
    except (ValueError, TypeError):
        return None


def _period_label(sport: str, period: int | None, detail: str) -> str | None:
    """Sport-specific human-readable period label."""
    if sport == "MLB":
        return detail or (f"Inning {period}" if period else None)
    if sport in ("NBA", "NFL"):
        if period and period > 4:
            return f"OT{period - 4}"
        return f"Q{period}" if period else detail or None
    if sport == "NHL":
        if period and period > 3:
            return "OT" if period == 4 else f"OT{period - 3}"
        return f"P{period}" if period else detail or None
    return detail or None


def extract_game_state(sport: str, espn_event: dict) -> dict:
    """Pull normalized game-state fields out of one ESPN event."""
    comp = (espn_event.get("competitions") or [{}])[0]
    status = comp.get("status") or {}
    type_ = status.get("type") or {}
    competitors = comp.get("competitors") or []
    away = next((c for c in competitors if c.get("homeAway") == "away"), {})
    home = next((c for c in competitors if c.get("homeAway") == "home"), {})
    away_team = (away.get("team") or {})
    home_team = (home.get("team") or {})
    period = _to_int_or_none(status.get("period"))

    return {
        "external_game_id": str(espn_event.get("id") or "") or None,
        "game_status": type_.get("state"),
        "game_status_detail": type_.get("detail") or type_.get("shortDetail"),
        "period": period,
        "period_label": _period_label(sport, period, type_.get("detail") or ""),
        "clock": status.get("displayClock"),
        "away_team": away_team.get("displayName"),
        "home_team": home_team.get("displayName"),
        "away_score": _to_int_or_none(away.get("score")),
        "home_score": _to_int_or_none(home.get("score")),
        "_away_keys": [
            (away_team.get("displayName") or "").lower(),
            (away_team.get("shortDisplayName") or "").lower(),
            (away_team.get("name") or "").lower(),
            (away_team.get("nickname") or "").lower(),
            (away_team.get("abbreviation") or "").lower(),
        ],
        "_home_keys": [
            (home_team.get("displayName") or "").lower(),
            (home_team.get("shortDisplayName") or "").lower(),
            (home_team.get("name") or "").lower(),
            (home_team.get("nickname") or "").lower(),
            (home_team.get("abbreviation") or "").lower(),
        ],
    }


# ---------- Matching Kalshi event -> ESPN event ----------

def _split_kalshi_title(title: str) -> tuple[str, str] | None:
    cleaned = GAME_PREFIX_RE.sub("", (title or "").strip())
    parts = re.split(r"\s+vs\.?\s+|\s+@\s+|\s+at\s+", cleaned, flags=re.IGNORECASE)
    if len(parts) != 2:
        return None
    a, b = parts[0].strip().lower(), parts[1].strip().lower()
    if not a or not b:
        return None
    return a, b


def _expand_aliases(sport: str, parsed_name: str) -> list[str]:
    """Return [parsed_name] + any aliases for this team. Each is a probe to try."""
    return [parsed_name] + TEAM_ALIASES.get(sport, {}).get(parsed_name, [])


def match_kalshi_to_espn(
    sport: str, event: dict, espn_cache: dict[tuple[str, str], list[dict]],
) -> tuple[dict | None, list[dict]]:
    """Find ESPN event matching the Kalshi event. Returns (matched, candidates_list).

    Candidates_list is the ESPN games for the parsed date — useful for logging
    when no match was found.
    """
    title = event.get("title") or ""
    et = event.get("event_ticker") or ""
    parsed = _split_kalshi_title(title)
    d = parse_kalshi_ticker_date(et)
    candidates: list[dict] = []

    # Try exact-date match FIRST. A Kalshi ticker dated 26MAY05 should match
    # ESPN's 26MAY05 games, not today's (otherwise tomorrow's BOS-DET event
    # gets stamped with today's "Bottom 2nd" state). Only fall back to
    # adjacent days if exact date returns zero candidates (possible TZ-edge
    # late games where the ESPN bucket is one day off).
    if d is not None:
        candidates = list(espn_cache.get((sport, d.isoformat()), []))
        if not candidates:
            for off in (-1, 1):
                candidates.extend(espn_cache.get((sport, (d + timedelta(days=off)).isoformat()), []))
    else:
        # Couldn't parse date — fall back to all cached days for this sport
        for (s, _), evs in espn_cache.items():
            if s == sport:
                candidates.extend(evs)

    if parsed is None or not candidates:
        return None, candidates

    a_probes = _expand_aliases(sport, parsed[0])
    b_probes = _expand_aliases(sport, parsed[1])

    def any_probe_hits(team_keys: list[str], probes: list[str]) -> bool:
        for k in team_keys:
            if not k:
                continue
            for p in probes:
                if not p:
                    continue
                if p in k or k in p:
                    return True
        return False

    for cand in candidates:
        state = extract_game_state(sport, cand)
        if (any_probe_hits(state["_away_keys"], a_probes) and any_probe_hits(state["_home_keys"], b_probes)) or \
           (any_probe_hits(state["_away_keys"], b_probes) and any_probe_hits(state["_home_keys"], a_probes)):
            return cand, candidates
    return None, candidates


# ---------- Kalshi side helpers ----------

def discover_kalshi_events_for_series(series_ticker: str) -> list[dict]:
    try:
        return kalshi_api.get_events(series_ticker, status="open")
    except Exception:
        logger.exception("Kalshi discovery failed for %s", series_ticker)
        return []


def fetch_event_markets(event_ticker: str) -> list[dict]:
    return kalshi_api.get_markets_for_event(event_ticker, status="open")


def _to_cents(v) -> int | None:
    try:
        if v is None or v == "":
            return None
        return int(round(float(v) * 100))
    except (ValueError, TypeError):
        return None


def _to_int(v) -> int | None:
    try:
        if v is None or v == "":
            return None
        return int(float(v))
    except (ValueError, TypeError):
        return None


def split_markets_by_team(markets: list[dict], state: dict | None) -> tuple[dict | None, dict | None]:
    """Identify which Kalshi market is the away vs home YES contract.

    Match by yes_sub_title (e.g. 'Cubs', 'Mets') against ESPN team keys.
    Falls back to first/second when there are exactly two markets.
    """
    if state is None:
        if len(markets) == 2:
            return markets[0], markets[1]
        return None, None

    away_keys = state["_away_keys"]
    home_keys = state["_home_keys"]
    away_m = home_m = None
    for m in markets:
        sub = (m.get("yes_sub_title") or m.get("subtitle") or "").lower()
        if not sub:
            continue
        for k in away_keys:
            if k and (sub in k or k in sub):
                away_m = m
                break
        else:
            for k in home_keys:
                if k and (sub in k or k in sub):
                    home_m = m
                    break

    if (away_m is None or home_m is None) and len(markets) == 2:
        # Fallback: keep what we matched, fill in the other side from leftovers
        leftover = [m for m in markets if m is not away_m and m is not home_m]
        if leftover and away_m is None:
            away_m = leftover[0]
        elif leftover and home_m is None:
            home_m = leftover[0]
    return away_m, home_m


def has_recent_snapshot(event_ticker: str, cooldown_seconds: int) -> bool:
    conn = db.get_connection(readonly=True)
    try:
        cutoff = time.time() - cooldown_seconds
        row = conn.execute(
            "SELECT 1 FROM sports_market_snapshots "
            "WHERE kalshi_event_ticker = ? AND timestamp >= ? LIMIT 1",
            (event_ticker, cutoff),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def _summarize_candidates(sport: str, cands: list[dict]) -> str:
    """JSON-encode candidate ESPN games for the failure log."""
    out = []
    for c in cands[:8]:
        comp = (c.get("competitions") or [{}])[0]
        teams = []
        for t in comp.get("competitors", []):
            tt = t.get("team") or {}
            teams.append({"name": tt.get("displayName"), "abbrev": tt.get("abbreviation")})
        out.append({"id": c.get("id"), "date": c.get("date"), "teams": teams})
    return json.dumps(out)


def record_match_failure(sport: str, event: dict, candidates: list[dict], reason: str) -> None:
    et = event.get("event_ticker") or ""
    title = event.get("title") or ""
    d = parse_kalshi_ticker_date(et)
    conn = db.get_connection()
    try:
        conn.execute(
            "INSERT INTO sports_match_failures "
            "(timestamp, sport, kalshi_event_ticker, kalshi_event_title, parsed_date, "
            "espn_candidates, reason) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (time.time(), sport, et, title, d.isoformat() if d else None,
             _summarize_candidates(sport, candidates), reason),
        )
        conn.commit()
    finally:
        conn.close()


def snapshot_event(
    sport: str, series_ticker: str, event: dict,
    espn_cache: dict, dry_run: bool = False,
) -> tuple[bool, bool]:
    """Take one snapshot of a Kalshi event. Returns (would_write, matched_to_espn)."""
    et = event.get("event_ticker") or ""
    if not et:
        return False, False

    espn_event, candidates = match_kalshi_to_espn(sport, event, espn_cache)
    state = extract_game_state(sport, espn_event) if espn_event else None

    # Pull current Kalshi market state for both sides
    try:
        markets = fetch_event_markets(et)
    except Exception:
        logger.debug("fetch_event_markets failed for %s", et)
        return False, espn_event is not None

    away_m, home_m = split_markets_by_team(markets, state)
    if away_m is None and home_m is None:
        # Truly nothing usable — record failure and skip
        if not dry_run and espn_event is None:
            record_match_failure(sport, event, candidates, "no_espn_match_and_no_markets")
        return False, False

    if dry_run:
        return True, espn_event is not None

    # On no-match, still record snapshot but with null game state
    if espn_event is None:
        record_match_failure(sport, event, candidates, "no_espn_match")

    away_team = state["away_team"] if state else None
    home_team = state["home_team"] if state else None
    game_date = (parse_kalshi_ticker_date(et) or date.today()).isoformat()

    row = (
        time.time(), sport, series_ticker, et, away_team, home_team, game_date,
        state["external_game_id"] if state else None,
        state["game_status"] if state else None,
        state["game_status_detail"] if state else None,
        state["period"] if state else None,
        state["period_label"] if state else None,
        state["clock"] if state else None,
        state["away_score"] if state else None,
        state["home_score"] if state else None,
        away_m.get("ticker") if away_m else None,
        home_m.get("ticker") if home_m else None,
        _to_cents(away_m.get("yes_ask_dollars")) if away_m else None,
        _to_cents(away_m.get("yes_bid_dollars")) if away_m else None,
        _to_int(away_m.get("yes_ask_size_fp")) if away_m else None,
        _to_cents(home_m.get("yes_ask_dollars")) if home_m else None,
        _to_cents(home_m.get("yes_bid_dollars")) if home_m else None,
        _to_int(home_m.get("yes_ask_size_fp")) if home_m else None,
    )
    conn = db.get_connection()
    try:
        conn.execute(
            "INSERT INTO sports_market_snapshots "
            "(timestamp, sport, series_ticker, kalshi_event_ticker, away_team, home_team, "
            "game_date, external_game_id, game_status, game_status_detail, period, "
            "period_label, clock, away_score, home_score, away_ticker, home_ticker, "
            "away_yes_ask, away_yes_bid, away_depth, home_yes_ask, home_yes_bid, home_depth) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            row,
        )
        conn.commit()
    finally:
        conn.close()
    return True, espn_event is not None


# ---------- Resolution ----------

def resolve_settled_games() -> int:
    """For events with no resolved_at, check Kalshi for settlement and write
    winner + final scores back to all that event's snapshot rows."""
    conn = db.get_connection(readonly=True)
    try:
        rows = conn.execute(
            "SELECT DISTINCT sport, kalshi_event_ticker, away_ticker, home_ticker, external_game_id "
            "FROM sports_market_snapshots WHERE resolved_at IS NULL"
        ).fetchall()
        unresolved = [dict(r) for r in rows]
    finally:
        conn.close()

    if not unresolved:
        return 0

    n_resolved = 0
    for u in unresolved:
        away_t, home_t = u.get("away_ticker"), u.get("home_ticker")
        sample = away_t or home_t
        if not sample:
            continue
        try:
            mkt = kalshi_api.get_market(sample)
        except Exception:
            continue
        if mkt.get("status") not in ("settled", "finalized"):
            continue

        away_result = home_result = None
        try:
            if away_t:
                away_result = (kalshi_api.get_market(away_t).get("result") or "").lower()
            if home_t:
                home_result = (kalshi_api.get_market(home_t).get("result") or "").lower()
        except Exception:
            pass

        winner = None
        if away_result == "yes" or home_result == "no":
            winner = "away"
        elif home_result == "yes" or away_result == "no":
            winner = "home"
        if winner is None:
            continue

        # Final scores: pull from the most recent snapshot's ESPN state if possible
        final_away = final_home = None
        sport = u.get("sport")
        if sport and u.get("external_game_id"):
            try:
                # Re-hit ESPN; the post-game scoreboard still carries final scores
                gid = u["external_game_id"]
                for d in get_lookup_dates():
                    for ev in fetch_espn_scoreboard(sport, d):
                        if str(ev.get("id")) == str(gid):
                            st = extract_game_state(sport, ev)
                            final_away, final_home = st.get("away_score"), st.get("home_score")
                            break
                    if final_away is not None or final_home is not None:
                        break
            except Exception:
                pass

        conn = db.get_connection()
        try:
            conn.execute(
                "UPDATE sports_market_snapshots SET resolved_at = ?, resolved_winner = ?, "
                "final_away_score = ?, final_home_score = ? "
                "WHERE kalshi_event_ticker = ? AND resolved_at IS NULL",
                (time.time(), winner, final_away, final_home, u["kalshi_event_ticker"]),
            )
            conn.commit()
        finally:
            conn.close()
        n_resolved += 1
        logger.warning("RESOLVED %s %s -> %s wins (final %s-%s)",
                       u.get("sport"), u["kalshi_event_ticker"], winner.upper(),
                       final_away, final_home)
    return n_resolved


# ---------- Cycle ----------

def run_dry_run() -> None:
    """Discover Kalshi events + ESPN scoreboards, attempt to match, print stats.

    Writes nothing to the DB. Used to verify match rates before going live.
    Splits "events whose date is inside our ESPN window" from "events outside
    the window" — only the in-window match rate is a real measure of quality.
    Out-of-window events (e.g. playoff Game 5 a week from now) cannot match
    because ESPN hasn't published those games yet; that's not a matching bug.
    """
    espn_cache = refresh_all_scoreboards()
    window_dates = {d.isoformat() for d in get_lookup_dates()}

    print("\n=== DRY RUN: Kalshi <-> ESPN match rate ===\n")
    print("ESPN window (days with scoreboard data fetched):")
    print(f"  {sorted(window_dates)[0]} .. {sorted(window_dates)[-1]} "
          f"({len(window_dates)} days)\n")

    total_in_match = total_in = total_out = 0
    per_sport_unmatched: dict[str, list[dict]] = {}
    for series, sport in KALSHI_SERIES_TO_SPORT.items():
        events = discover_kalshi_events_for_series(series)
        in_match = in_unmatch = out_window = 0
        unmatched_rows: list[dict] = []
        for ev in events:
            d = parse_kalshi_ticker_date(ev.get("event_ticker") or "")
            in_window = d is not None and d.isoformat() in window_dates
            espn_event, cands = match_kalshi_to_espn(sport, ev, espn_cache)
            if espn_event is not None:
                if in_window:
                    in_match += 1
                else:
                    in_match += 1  # matched anyway via fuzzy date window
            else:
                if in_window:
                    in_unmatch += 1
                    unmatched_rows.append({
                        "ticker": ev.get("event_ticker"),
                        "title": ev.get("title"),
                        "parsed_date": d.isoformat() if d else None,
                        "n_candidates": len(cands),
                    })
                else:
                    out_window += 1
        per_sport_unmatched[sport] = unmatched_rows
        total = in_match + in_unmatch + out_window
        total_in_match += in_match
        total_in += in_match + in_unmatch
        total_out += out_window
        in_rate = (100.0 * in_match / max(in_match + in_unmatch, 1))
        print(f"  {sport:5} | events={total:3d}  in_window={in_match + in_unmatch:3d}  "
              f"matched={in_match:3d}  unmatched={in_unmatch:3d}  "
              f"out_of_window={out_window:3d}  in_window_rate={in_rate:5.1f}%")
    overall = (100.0 * total_in_match / max(total_in, 1))
    print(f"\n  TOTAL | in_window={total_in}  matched={total_in_match}  "
          f"out_of_window={total_out}  in_window_match_rate={overall:.1f}%\n")
    print("(out_of_window events are dated outside the ESPN window — usually\n"
          " conditional playoff games not yet published. Not a matching defect.)\n")

    print("=== Sample IN-WINDOW unmatched (these are real defects, first 5 per sport) ===\n")
    any_unmatched = False
    for sport, rows in per_sport_unmatched.items():
        if not rows:
            continue
        any_unmatched = True
        print(f"-- {sport} --")
        for r in rows[:5]:
            print(f"  {r['ticker']!s:40} title={r['title']!r:50} "
                  f"date={r['parsed_date']} cands={r['n_candidates']}")
        print()
    if not any_unmatched:
        print("  (none — every Kalshi event in the ESPN window matched)\n")
    print("=== End dry-run ===\n")


def cycle(
    espn_cache: dict, snapshot_interval: int, dry_run: bool = False,
) -> tuple[int, int, int]:
    """One pass over all events. Returns (n_written, n_matched, n_total)."""
    cooldown = max(snapshot_interval - 30, 60)
    n_written = n_matched = n_total = 0
    for series, sport in KALSHI_SERIES_TO_SPORT.items():
        events = discover_kalshi_events_for_series(series)
        for ev in events:
            n_total += 1
            et = ev.get("event_ticker") or ""
            if not et:
                continue
            if not dry_run and has_recent_snapshot(et, cooldown):
                continue
            try:
                wrote, matched = snapshot_event(sport, series, ev, espn_cache, dry_run=dry_run)
                if wrote:
                    n_written += 1
                if matched:
                    n_matched += 1
            except Exception:
                logger.exception("snapshot_event failed for %s/%s", sport, et)
    return n_written, n_matched, n_total


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--once", action="store_true", help="Run one cycle then exit")
    ap.add_argument("--dry-run", action="store_true",
                    help="Don't write to DB; print Kalshi/ESPN match-rate report")
    ap.add_argument("--resolve-only", action="store_true",
                    help="Only check resolutions on existing snapshots")
    ap.add_argument("--snapshot-interval", type=int, default=SNAPSHOT_INTERVAL_SECONDS)
    ap.add_argument("--discovery-refresh", type=int, default=DISCOVERY_REFRESH_SECONDS)
    ap.add_argument("--scoreboard-refresh", type=int, default=SCOREBOARD_REFRESH_SECONDS)
    ap.add_argument("--resolution-interval", type=int, default=RESOLUTION_INTERVAL_SECONDS)
    args = ap.parse_args()

    setup_logging()
    db_logger.init_db()
    init_schema()

    if args.dry_run:
        run_dry_run()
        return

    if args.resolve_only:
        n = resolve_settled_games()
        logger.info("resolve-only complete: %d events resolved", n)
        return

    espn_cache = refresh_all_scoreboards()
    last_scoreboard = time.time()
    last_resolution = 0.0

    if args.once:
        n_w, n_m, n_t = cycle(espn_cache, args.snapshot_interval)
        m = resolve_settled_games()
        logger.info("once complete: events=%d matched=%d snapshots_written=%d resolved=%d",
                    n_t, n_m, n_w, m)
        return

    logger.info("Sports snapshot collector running | snapshot_every=%ds sports=%s",
                args.snapshot_interval, list(KALSHI_SERIES_TO_SPORT.values()))
    while True:
        try:
            now = time.time()
            if now - last_scoreboard > args.scoreboard_refresh:
                espn_cache = refresh_all_scoreboards()
                last_scoreboard = now

            n_w, n_m, n_t = cycle(espn_cache, args.snapshot_interval)
            if n_w or n_t:
                logger.info("cycle: events=%d matched=%d snapshots_written=%d",
                            n_t, n_m, n_w)

            if now - last_resolution > args.resolution_interval:
                resolve_settled_games()
                last_resolution = now
        except KeyboardInterrupt:
            logger.info("Shutdown")
            break
        except Exception:
            logger.exception("loop error — continuing")
        time.sleep(min(args.snapshot_interval, 60))


if __name__ == "__main__":
    main()
