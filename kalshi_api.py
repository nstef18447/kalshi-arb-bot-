from auth import authenticated_request


def get_markets(series_ticker: str, status: str = "open") -> list[dict]:
    """Fetch all markets for a series with given status."""
    markets = []
    cursor = None

    while True:
        params = {
            "series_ticker": series_ticker,
            "status": status,
            "limit": 200,
        }
        if cursor:
            params["cursor"] = cursor

        data = authenticated_request("GET", "/trade-api/v2/markets", params=params)
        markets.extend(data.get("markets", []))
        cursor = data.get("cursor")
        if not cursor:
            break

    return markets


def get_orderbook(ticker: str, depth: int = 1) -> dict:
    """Fetch orderbook for a specific ticker.

    Returns dict with 'yes' and 'no' keys, each containing list of [price, quantity] levels.
    """
    data = authenticated_request(
        "GET", f"/trade-api/v2/orderbook/{ticker}", params={"depth": depth}
    )
    return data.get("orderbook", {})


def create_order(ticker: str, side: str, price_cents: int, count: int) -> dict:
    """Place a limit order.

    Args:
        ticker: Market ticker
        side: 'yes' or 'no'
        price_cents: Limit price in cents (1-99)
        count: Number of contracts

    Returns:
        Order dict from API
    """
    body = {
        "ticker": ticker,
        "action": "buy",
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
    """Cancel an open order."""
    authenticated_request("DELETE", f"/trade-api/v2/portfolio/orders/{order_id}")


def get_positions() -> list[dict]:
    """Fetch all current positions."""
    data = authenticated_request("GET", "/trade-api/v2/portfolio/positions")
    return data.get("market_positions", [])


def get_balance() -> int:
    """Fetch account balance in cents."""
    data = authenticated_request("GET", "/trade-api/v2/portfolio/balance")
    return data.get("balance", 0)
