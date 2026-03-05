"""Quick check: real bid/ask spread on KXBTC15M market endpoint."""
import time
from dotenv import load_dotenv
load_dotenv()

import kalshi_api
from auth import authenticated_request

ticker = None
for i in range(30):
    markets = kalshi_api.get_markets("KXBTC15M", status="open")
    if not markets:
        print(f"#{i}: no markets")
        time.sleep(0.5)
        continue
    t = markets[0]["ticker"]
    if t != ticker:
        ticker = t
        print(f"--- Ticker: {ticker} ---")

    m_data = authenticated_request("GET", f"/trade-api/v2/markets/{ticker}")
    m = m_data.get("market", {})
    ya = m.get("yes_ask", 0)
    yb = m.get("yes_bid", 0)
    na = m.get("no_ask", 0)
    nb = m.get("no_bid", 0)

    buy_both = ya + na
    sell_both = yb + nb
    yes_spread = ya - yb
    no_spread = na - nb
    print(
        f"  #{i:2d} ya={ya:3d} yb={yb:3d} na={na:3d} nb={nb:3d} | "
        f"buy_both={buy_both:3d} sell_both={sell_both:3d} "
        f"y_spr={yes_spread} n_spr={no_spread}"
    )
    time.sleep(0.5)
