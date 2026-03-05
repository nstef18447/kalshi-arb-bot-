import logging

import config
from auth import authenticated_request

logger = logging.getLogger("arb-bot")


def _paginate(path: str, params: dict, items_key: str) -> list[dict]:
    """Paginate through all results for a given endpoint."""
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


def get_events(series_ticker: str, status: str = "open") -> list[dict]:
    """Fetch all events (expiry windows) for a series."""
    return _paginate(
        "/trade-api/v2/events",
        {"series_ticker": series_ticker, "status": status, "limit": 200},
        "events",
    )


def get_markets_for_event(event_ticker: str, status: str = "open") -> list[dict]:
    """Fetch all markets (strikes) for a specific event."""
    return _paginate(
        "/trade-api/v2/markets",
        {"event_ticker": event_ticker, "status": status, "limit": 200},
        "markets",
    )


def get_markets(series_ticker: str, status: str = "open") -> list[dict]:
    """Fetch ALL markets across ALL active expiry windows for a series.

    Uses the events API first to discover all active windows, then fetches
    markets per event. Falls back to direct series_ticker query if events
    endpoint returns nothing.
    """
    # Primary approach: events -> markets per event
    events = get_events(series_ticker, status)
    if events:
        all_markets = []
        for ev in events:
            event_ticker = ev.get("event_ticker", "")
            if not event_ticker:
                continue
            markets = get_markets_for_event(event_ticker, status)
            all_markets.extend(markets)
        if all_markets:
            logger.debug(
                "Fetched %d markets across %d events for %s",
                len(all_markets), len(events), series_ticker,
            )
            return all_markets

    # Fallback: direct series_ticker on markets endpoint
    markets = _paginate(
        "/trade-api/v2/markets",
        {"series_ticker": series_ticker, "status": status, "limit": 200},
        "markets",
    )
    logger.debug(
        "Fallback: fetched %d markets via series_ticker=%s",
        len(markets), series_ticker,
    )
    return markets


def get_market(ticker: str) -> dict:
    """Fetch a single market's details (including result after settlement)."""
    data = authenticated_request("GET", f"/trade-api/v2/markets/{ticker}")
    return data.get("market", {})


def get_orderbook(ticker: str, depth: int = 1) -> dict:
    """Fetch orderbook for a specific ticker.

    Returns dict with 'yes' and 'no' keys, each containing list of [price, quantity] levels.
    """
    data = authenticated_request(
        "GET", f"/trade-api/v2/markets/{ticker}/orderbook", params={"depth": depth}
    )
    return data.get("orderbook", {})


def create_order(ticker: str, side: str, price_cents: int, count: int,
                  post_only: bool = False) -> dict:
    """Place a limit order. Blocked when READ_ONLY is true."""
    if config.READ_ONLY:
        raise RuntimeError("create_order blocked: READ_ONLY mode is active")
    body = {
        "ticker": ticker,
        "action": "buy",
        "side": side,
        "type": "limit",
        "yes_price" if side == "yes" else "no_price": price_cents,
        "count": count,
    }
    if post_only:
        body["post_only"] = True
    data = authenticated_request("POST", "/trade-api/v2/portfolio/orders", json_body=body)
    return data.get("order", {})


def create_sell_order(ticker: str, side: str, price_cents: int, count: int) -> dict:
    """Place a limit sell order. Blocked when READ_ONLY is true."""
    if config.READ_ONLY:
        raise RuntimeError("create_sell_order blocked: READ_ONLY mode is active")
    body = {
        "ticker": ticker,
        "action": "sell",
        "side": side,
        "type": "limit",
        "yes_price" if side == "yes" else "no_price": price_cents,
        "count": count,
    }
    data = authenticated_request("POST", "/trade-api/v2/portfolio/orders", json_body=body)
    return data.get("order", {})


def get_order(order_id: str) -> dict:
    """Get order details including fill status."""
    data = authenticated_request("GET", f"/trade-api/v2/portfolio/orders/{order_id}")
    return data.get("order", {})


def cancel_order(order_id: str) -> None:
    """Cancel an open order. Blocked when READ_ONLY is true."""
    if config.READ_ONLY:
        raise RuntimeError("cancel_order blocked: READ_ONLY mode is active")
    authenticated_request("DELETE", f"/trade-api/v2/portfolio/orders/{order_id}")


def get_open_orders() -> list[dict]:
    """Fetch all resting (open) orders."""
    return _paginate(
        "/trade-api/v2/portfolio/orders",
        {"status": "resting", "limit": 200},
        "orders",
    )


def get_positions() -> list[dict]:
    """Fetch all current positions."""
    data = authenticated_request("GET", "/trade-api/v2/portfolio/positions")
    return data.get("market_positions", [])


def get_balance() -> int:
    """Fetch account balance in cents."""
    data = authenticated_request("GET", "/trade-api/v2/portfolio/balance")
    return data.get("balance", 0)
