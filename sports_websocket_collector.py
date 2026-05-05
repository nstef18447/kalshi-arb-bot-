"""Kalshi WebSocket tick collector for sports markets (MLB / NHL / NBA / NFL).

Parallel to sports_snapshot_collector.py (which polls every 5 minutes). This
script subscribes to Kalshi's orderbook_delta channel for every active market
in the four major-sport money-line series and writes every update it receives
to the sports_market_ticks table.

Why a separate collector: the polling collector samples too coarsely to
backtest exit timing or fill probability; this one captures every top-of-book
change, giving us tick-level data for the same markets.

Auth: same RSA-PSS scheme as REST — see auth._sign_request. The WS handshake
includes KALSHI-ACCESS-KEY/SIGNATURE/TIMESTAMP headers signed against
"GET" + "/trade-api/ws/v2".

Endpoint: driven by KALSHI_ENV (prod -> wss://api.elections.kalshi.com,
demo -> wss://demo-api.kalshi.co). Defaults to demo if unset.

Run modes:
  - long-running:  python sports_websocket_collector.py
  - smoke test:    python sports_websocket_collector.py --seconds 60
"""

import argparse
import json
import logging
import os
import signal
import sys
import threading
import time
from typing import Optional

import websocket  # websocket-client (sync)
from dotenv import load_dotenv

# Local .env loads first. If a side-channel `.env.droplet` is present we let
# it override KALSHI_API_KEY / KALSHI_PRIVATE_KEY_PATH — useful when the
# local API key lacks WS / portfolio access (the WS API requires an
# account-bound key) but a droplet key sitting next to the project does.
load_dotenv()
_DROPLET_ENV = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env.droplet")
if os.path.exists(_DROPLET_ENV):
    load_dotenv(_DROPLET_ENV, override=True)

import db
import db_logger
import kalshi_api
from auth import _sign_request

logger = logging.getLogger("sports_ws")

# Kalshi money-line series we track. Mirror sports_snapshot_collector.
KALSHI_SERIES_TO_SPORT = {
    "KXMLBGAME": "MLB",
    "KXNHLGAME": "NHL",
    "KXNBAGAME": "NBA",
    "KXNFLGAME": "NFL",
}

# Per-subscribe ticker batch size. Kalshi has historically capped this; 100
# is a safe default. If a subscribe is rejected we back off in halves.
DEFAULT_BATCH_SIZE = 100

# WS path used for both signing and the URL.
WS_PATH = "/trade-api/ws/v2"
PROD_WS_HOST = "wss://api.elections.kalshi.com"
DEMO_WS_HOST = "wss://demo-api.kalshi.co"

# Refresh the discovered ticker set this often. Games come and go; we
# resubscribe when the set changes.
DISCOVERY_REFRESH_SECONDS = 600

# Reconnect backoff bounds.
RECONNECT_BACKOFF_MIN = 1.0
RECONNECT_BACKOFF_MAX = 60.0


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sports_market_ticks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    ticker TEXT NOT NULL,
    event_ticker TEXT,
    sport TEXT,
    yes_bid INTEGER,
    yes_ask INTEGER,
    yes_bid_depth INTEGER,
    yes_ask_depth INTEGER,
    no_bid INTEGER,
    no_ask INTEGER,
    raw_msg_type TEXT
);
CREATE INDEX IF NOT EXISTS idx_sports_ticks_ticker ON sports_market_ticks(ticker);
CREATE INDEX IF NOT EXISTS idx_sports_ticks_ts ON sports_market_ticks(timestamp);
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


def _get_ws_url() -> str:
    env = os.getenv("KALSHI_ENV", "demo").lower()
    host = PROD_WS_HOST if env == "prod" else DEMO_WS_HOST
    return f"{host}{WS_PATH}"


def _build_auth_headers() -> list[str]:
    """Build the list of HTTP headers (websocket-client wants list of 'K: V' strings)."""
    api_key = os.getenv("KALSHI_API_KEY", "")
    timestamp_ms = int(time.time() * 1000)
    signature = _sign_request("GET", WS_PATH, timestamp_ms)
    return [
        f"KALSHI-ACCESS-KEY: {api_key}",
        f"KALSHI-ACCESS-SIGNATURE: {signature}",
        f"KALSHI-ACCESS-TIMESTAMP: {timestamp_ms}",
    ]


# ---------- Discovery ----------

def discover_tickers() -> tuple[list[str], dict[str, tuple[str, str]]]:
    """Return (ticker_list, ticker_meta).

    ticker_meta maps market_ticker -> (event_ticker, sport) so we can stamp
    each tick row with the sport/event without another lookup.
    """
    tickers: list[str] = []
    meta: dict[str, tuple[str, str]] = {}
    for series, sport in KALSHI_SERIES_TO_SPORT.items():
        try:
            events = kalshi_api.get_events(series, status="open")
        except Exception:
            logger.exception("Kalshi events discovery failed for %s", series)
            continue
        for ev in events:
            et = ev.get("event_ticker") or ""
            if not et:
                continue
            try:
                markets = kalshi_api.get_markets_for_event(et, status="open")
            except Exception:
                logger.exception("Markets fetch failed for %s", et)
                continue
            for m in markets:
                t = m.get("ticker")
                if not t:
                    continue
                tickers.append(t)
                meta[t] = (et, sport)
    # Dedupe but preserve order
    seen: set[str] = set()
    deduped = []
    for t in tickers:
        if t not in seen:
            seen.add(t)
            deduped.append(t)
    return deduped, meta


# ---------- Tick decoding ----------

def _dollars_to_cents(v) -> Optional[int]:
    """Convert a Kalshi price-in-dollars string (e.g. '0.4700') to cents (47).

    Some snapshots use raw cents-as-string; we accept both.
    """
    if v is None or v == "":
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    # If it looks like dollars (0..1), scale; else assume already cents.
    if -1.5 <= f <= 1.5:
        return int(round(f * 100))
    return int(round(f))


def _to_int_size(v) -> Optional[int]:
    """Sizes come as fixed-point strings — round to int contracts."""
    if v is None or v == "":
        return None
    try:
        return int(round(float(v)))
    except (TypeError, ValueError):
        return None


class OrderbookState:
    """Minimal in-memory orderbook for one ticker so we can apply
    orderbook_delta updates incrementally.

    Kalshi WS message shapes (verified live, 2026-05):
      - orderbook_snapshot
        msg.yes_dollars_fp = [[price_dollars_str, size_fp_str], ...]
        msg.no_dollars_fp  = [[price_dollars_str, size_fp_str], ...]
      - orderbook_delta
        msg.price_dollars  = "0.4700"
        msg.delta_fp       = "-45.66"  (NB: can be fractional)
        msg.side           = "yes" | "no"
        msg.seq            = monotonically increasing per (sid)

    We store each side as {price_cents: size_int}. Top-of-book per side is the
    highest price (a Kalshi binary book stores BIDs only — to sell yes you
    cross the no side at price = 100 - no_bid).
    """

    __slots__ = ("yes", "no")

    def __init__(self):
        self.yes: dict[int, int] = {}
        self.no: dict[int, int] = {}

    def apply_snapshot(self, msg: dict) -> None:
        self.yes = {}
        self.no = {}
        # Field names verified live; fall back to alternates in case the spec
        # changes again.
        yes_levels = msg.get("yes_dollars_fp") or msg.get("yes") or []
        no_levels = msg.get("no_dollars_fp") or msg.get("no") or []
        for level in yes_levels:
            try:
                p = _dollars_to_cents(level[0])
                s = _to_int_size(level[1])
            except (IndexError, TypeError):
                continue
            if p is not None and s is not None and s > 0:
                self.yes[p] = s
        for level in no_levels:
            try:
                p = _dollars_to_cents(level[0])
                s = _to_int_size(level[1])
            except (IndexError, TypeError):
                continue
            if p is not None and s is not None and s > 0:
                self.no[p] = s

    def apply_delta(self, msg: dict) -> None:
        side = msg.get("side")
        price = _dollars_to_cents(msg.get("price_dollars") or msg.get("price"))
        delta = _to_int_size(msg.get("delta_fp") or msg.get("delta"))
        if side is None or price is None or delta is None:
            return
        book = self.yes if side == "yes" else self.no if side == "no" else None
        if book is None:
            return
        new = book.get(price, 0) + delta
        if new <= 0:
            book.pop(price, None)
        else:
            book[price] = new

    def best(self) -> tuple[Optional[int], Optional[int], Optional[int], Optional[int]]:
        """Return (yes_best_bid, yes_best_size, no_best_bid, no_best_size)."""
        yp = max(self.yes) if self.yes else None
        ys = self.yes.get(yp) if yp is not None else None
        np_ = max(self.no) if self.no else None
        ns = self.no.get(np_) if np_ is not None else None
        return yp, ys, np_, ns


# ---------- Main collector ----------

class WSCollector:
    def __init__(self, run_seconds: Optional[int] = None,
                 batch_size: int = DEFAULT_BATCH_SIZE):
        self.run_seconds = run_seconds
        self.batch_size = batch_size
        self.ws: Optional[websocket.WebSocketApp] = None

        self.tickers: list[str] = []
        self.meta: dict[str, tuple[str, str]] = {}
        self.books: dict[str, OrderbookState] = {}
        self.sid_to_ticker: dict[int, str] = {}  # subscription id -> first ticker (for debugging)

        self._next_id = 1
        self._lock = threading.Lock()
        self._stop = threading.Event()

        # Counters
        self.ticks_received = 0
        self.rows_written = 0
        self.connect_count = 0
        self.last_subscribe_ok = False

        # Per-connection subscription bookkeeping
        self._pending_batches: list[list[str]] = []
        self._sent_batch_count = 0

    # --- lifecycle ---

    def stop(self) -> None:
        self._stop.set()
        if self.ws is not None:
            try:
                self.ws.close()
            except Exception:
                pass

    def _next_msg_id(self) -> int:
        with self._lock:
            i = self._next_id
            self._next_id += 1
            return i

    # --- handlers ---

    def on_open(self, ws: websocket.WebSocketApp) -> None:
        self.connect_count += 1
        logger.info("WS connected (#%d). Sending %d subscribe batches for %d tickers.",
                    self.connect_count, len(self._pending_batches), len(self.tickers))
        for batch in self._pending_batches:
            try:
                payload = {
                    "id": self._next_msg_id(),
                    "cmd": "subscribe",
                    "params": {
                        "channels": ["orderbook_delta"],
                        "market_tickers": batch,
                    },
                }
                ws.send(json.dumps(payload))
                self._sent_batch_count += 1
            except Exception:
                logger.exception("Subscribe send failed")

    def on_message(self, ws: websocket.WebSocketApp, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except Exception:
            logger.debug("non-JSON ws msg: %r", raw[:200])
            return

        mtype = msg.get("type")

        # Subscribe ack / error
        if mtype == "subscribed":
            self.last_subscribe_ok = True
            sid = msg.get("msg", {}).get("sid")
            channel = msg.get("msg", {}).get("channel")
            logger.info("subscribed sid=%s channel=%s", sid, channel)
            return
        if mtype == "error":
            logger.warning("WS error msg: %s", msg)
            return

        # Orderbook messages — payload is in msg["msg"]
        body = msg.get("msg") or {}
        ticker = body.get("market_ticker") or body.get("ticker")
        if not ticker:
            return

        book = self.books.setdefault(ticker, OrderbookState())
        if mtype == "orderbook_snapshot":
            book.apply_snapshot(body)
        elif mtype == "orderbook_delta":
            book.apply_delta(body)
        else:
            return

        self.ticks_received += 1
        yp, ys, np_, ns = book.best()
        # yes_ask is the best price someone will sell yes at = 100 - best no-bid
        yes_ask = (100 - np_) if np_ is not None else None
        no_ask = (100 - yp) if yp is not None else None

        et, sport = self.meta.get(ticker, (None, None))
        try:
            conn = db.get_connection()
            try:
                conn.execute(
                    "INSERT INTO sports_market_ticks "
                    "(timestamp, ticker, event_ticker, sport, yes_bid, yes_ask, "
                    "yes_bid_depth, yes_ask_depth, no_bid, no_ask, raw_msg_type) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        time.time(), ticker, et, sport,
                        yp, yes_ask, ys, ns,
                        np_, no_ask, mtype,
                    ),
                )
                conn.commit()
                self.rows_written += 1
            finally:
                conn.close()
        except Exception:
            logger.exception("DB write failed for %s", ticker)

    def on_error(self, ws: websocket.WebSocketApp, err) -> None:
        logger.warning("WS error: %s", err)

    def on_close(self, ws: websocket.WebSocketApp, status_code, reason) -> None:
        logger.info("WS closed status=%s reason=%s", status_code, reason)

    # --- subscription planning ---

    def _build_batches(self) -> list[list[str]]:
        return [self.tickers[i:i + self.batch_size]
                for i in range(0, len(self.tickers), self.batch_size)]

    # --- run loop with reconnect ---

    def run(self) -> None:
        logger.info("Discovering active sports markets...")
        self.tickers, self.meta = discover_tickers()
        logger.info("Discovered %d markets across %d sports",
                    len(self.tickers), len(KALSHI_SERIES_TO_SPORT))
        if not self.tickers:
            logger.warning("No tickers found — nothing to subscribe to. Exiting.")
            return

        self._pending_batches = self._build_batches()
        url = _get_ws_url()
        logger.info("WS endpoint: %s", url)

        deadline = (time.time() + self.run_seconds) if self.run_seconds else None
        backoff = RECONNECT_BACKOFF_MIN

        while not self._stop.is_set():
            if deadline and time.time() >= deadline:
                break

            headers = _build_auth_headers()
            self._sent_batch_count = 0
            self.last_subscribe_ok = False
            self.ws = websocket.WebSocketApp(
                url,
                header=headers,
                on_open=self.on_open,
                on_message=self.on_message,
                on_error=self.on_error,
                on_close=self.on_close,
            )
            try:
                # ping_interval keeps the socket alive; the lib sends pings.
                # run_forever blocks until close. We bound it via deadline by
                # closing the socket from a watchdog thread.
                watchdog = None
                if deadline:
                    def _watchdog():
                        remaining = max(0.0, deadline - time.time())
                        if self._stop.wait(timeout=remaining):
                            return
                        try:
                            self.ws.close()
                        except Exception:
                            pass
                    watchdog = threading.Thread(target=_watchdog, daemon=True)
                    watchdog.start()

                # We don't send client pings — Kalshi pushes data continuously
                # while connected and the TCP keepalive carries us. Sending
                # pings produced "ping/pong timed out" disconnects in testing.
                self.ws.run_forever()
            except Exception:
                logger.exception("ws.run_forever crashed")

            if self._stop.is_set() or (deadline and time.time() >= deadline):
                break

            sleep_for = backoff
            logger.info("Reconnecting in %.1fs", sleep_for)
            if self._stop.wait(timeout=sleep_for):
                break
            backoff = min(backoff * 2, RECONNECT_BACKOFF_MAX)

        logger.info(
            "Run complete. Connected to %d markets. Received %d ticks. Wrote %d rows.",
            len(self.tickers), self.ticks_received, self.rows_written,
        )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seconds", type=int, default=None,
                    help="Run for N seconds, then exit (verification mode).")
    ap.add_argument("--once", action="store_true",
                    help="Alias for --seconds 60.")
    ap.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE,
                    help="Tickers per subscribe message (default %d)" % DEFAULT_BATCH_SIZE)
    args = ap.parse_args()

    setup_logging()
    db_logger.init_db()
    init_schema()

    run_seconds = args.seconds
    if args.once and run_seconds is None:
        run_seconds = 60

    collector = WSCollector(run_seconds=run_seconds, batch_size=args.batch_size)

    def _sigint(signum, frame):
        logger.info("SIGINT received — shutting down.")
        collector.stop()

    signal.signal(signal.SIGINT, _sigint)
    try:
        signal.signal(signal.SIGTERM, _sigint)
    except (AttributeError, ValueError):
        pass

    collector.run()


if __name__ == "__main__":
    main()
