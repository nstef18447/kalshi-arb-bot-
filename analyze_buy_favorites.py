"""Backtest: hypothetical BUY-favorite P&L on existing maker fills at >=95c.

Reads from maker_paper_orders (read-only). The original strategy SOLD YES at high
prices; this script asks: what if we had BOUGHT instead, given the same fills?

Outputs a structured report ending in a GO / NO-GO decision against fixed thresholds.
"""
import argparse
import io
import math
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone

from bot import _maker_fee
from config import SERIES

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

DEFAULT_MAKER_MULT = 0.0175
MIN_HOLD_DAYS = 1.0 / 24.0  # floor at 1 hour to avoid APR explosion on instant resolutions

# Decision-rule thresholds
GO_APR_MIN = 0.30
GO_WIN_RATE_MIN = 0.96
GO_WORST_DAY_PCT_MAX = 0.05
GO_CATEGORY_CONCENTRATION_MAX = 0.60


def maker_mult_for(series_ticker: str) -> float:
    cfg = SERIES.get(series_ticker)
    if cfg and "maker_mult" in cfg:
        return cfg["maker_mult"]
    return DEFAULT_MAKER_MULT


def buy_side_fee_cents(price_cents: int, signal_type: str, series_ticker: str) -> float:
    """Pick the right fee for the BUY direction based on what the row represents.

    sports_favorite_buy_taker rows simulate an instant taker buy, billed under
    Kalshi's general schedule: round_up(0.07 * P * (1-P)) cents.

    All other rows are treated as maker fills (the original SELL-side fills,
    inverted). Maker fees apply only to series listed in config.SERIES; rows on
    series not in that dict are assumed to fall under the general schedule
    where resting orders are free.
    """
    if signal_type and signal_type.startswith("sports_favorite_buy"):
        p = price_cents / 100.0
        return float(math.ceil(0.07 * p * (1.0 - p) * 100))
    if series_ticker in SERIES:
        return _maker_fee(price_cents, SERIES[series_ticker]["maker_mult"])
    return 0.0


def fmt_money(cents: float) -> str:
    return f"${cents/100:+.2f}"


def fmt_pct(x: float) -> str:
    return f"{x*100:+.1f}%"


def price_bucket(price: int) -> str:
    if price <= 96:
        return "95-96"
    if price == 97:
        return "97"
    if price == 98:
        return "98"
    return "99+"


def aggregate(rows: list[dict]) -> dict:
    if not rows:
        return {
            "n": 0, "wins": 0, "win_rate": 0.0,
            "total_pnl": 0.0, "total_capital": 0.0,
            "total_dollar_days": 0.0, "avg_apr_capital_weighted": 0.0,
            "avg_hold_days": 0.0,
        }
    n = len(rows)
    wins = sum(1 for r in rows if r["buy_pnl"] > 0)
    total_pnl = sum(r["buy_pnl"] for r in rows)
    total_capital = sum(r["fill_price"] for r in rows)
    total_dollar_days = sum(r["fill_price"] * r["hold_days"] for r in rows)
    # buy_pnl and fill_price are both in cents, so units cancel: pnl/(price*days) = 1/days
    pnl_per_dollar_day = (total_pnl / total_dollar_days) if total_dollar_days else 0.0
    avg_apr_cw = pnl_per_dollar_day * 365  # decimal, e.g. 0.30 = 30% APR
    avg_hold = sum(r["hold_days"] for r in rows) / n
    return {
        "n": n,
        "wins": wins,
        "win_rate": wins / n,
        "total_pnl": total_pnl,
        "total_capital": total_capital,
        "total_dollar_days": total_dollar_days,
        "avg_apr_capital_weighted": avg_apr_cw,
        "avg_hold_days": avg_hold,
    }


def print_agg_line(label: str, agg: dict, width: int = 28):
    if agg["n"] == 0:
        print(f"  {label:<{width}} | n=0")
        return
    print(
        f"  {label:<{width}} | n={agg['n']:5d} | WR {agg['win_rate']*100:5.1f}% | "
        f"P&L {fmt_money(agg['total_pnl']):>9s} | "
        f"APR {fmt_pct(agg['avg_apr_capital_weighted']):>7s} | "
        f"hold {agg['avg_hold_days']:5.1f}d"
    )


def tail_metrics(rows: list[dict]) -> dict:
    if not rows:
        return {"worst_day_pnl": 0.0, "worst_day": None, "max_drawdown": 0.0,
                "longest_loss_streak": 0, "worst_day_pct_of_capital": 0.0}
    by_day = defaultdict(float)
    for r in rows:
        d = datetime.fromtimestamp(r["resolved_at"], tz=timezone.utc).date()
        by_day[d] += r["buy_pnl"]
    worst_day, worst_day_pnl = min(by_day.items(), key=lambda kv: kv[1])

    # Max drawdown on cumulative P&L sorted by resolution time
    rows_sorted = sorted(rows, key=lambda r: r["resolved_at"])
    cum = 0.0
    peak = 0.0
    max_dd = 0.0
    for r in rows_sorted:
        cum += r["buy_pnl"]
        peak = max(peak, cum)
        dd = peak - cum
        max_dd = max(max_dd, dd)

    # Longest losing streak
    longest = cur = 0
    for r in rows_sorted:
        if r["buy_pnl"] <= 0:
            cur += 1
            longest = max(longest, cur)
        else:
            cur = 0

    total_capital = sum(r["fill_price"] for r in rows)
    worst_day_pct = abs(worst_day_pnl) / total_capital if total_capital else 0.0

    return {
        "worst_day_pnl": worst_day_pnl,
        "worst_day": worst_day,
        "max_drawdown": max_dd,
        "longest_loss_streak": longest,
        "worst_day_pct_of_capital": worst_day_pct,
    }


def category_concentration(rows: list[dict]) -> tuple[str, float]:
    by_cat = defaultdict(float)
    for r in rows:
        by_cat[r["category"] or "?"] += r["buy_pnl"]
    total = sum(abs(v) for v in by_cat.values())
    if total == 0:
        return ("none", 0.0)
    top_cat, top_pnl = max(by_cat.items(), key=lambda kv: abs(kv[1]))
    return (top_cat, abs(top_pnl) / total)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="arb_bot_droplet.db")
    ap.add_argument("--min-price", type=int, default=95)
    ap.add_argument("--realistic-haircut", type=float, default=0.20)
    ap.add_argument("--pessimistic-haircut", type=float, default=0.05)
    args = ap.parse_args()

    conn = sqlite3.connect(args.db, timeout=5)
    conn.row_factory = sqlite3.Row

    raw = conn.execute("""
        SELECT fill_price, resolved_price, series_ticker, category, event_ticker,
               bucket_label, filled_at, resolved_at, signal_type, filter_version,
               pnl_cents AS sell_pnl_cents
        FROM maker_paper_orders
        WHERE fill_price IS NOT NULL
          AND resolved_price IS NOT NULL
          AND resolved_at IS NOT NULL
          AND filled_at IS NOT NULL
          AND fill_price >= ?
    """, (args.min_price,)).fetchall()
    conn.close()

    if not raw:
        print(f"No filled+resolved rows at fill_price >= {args.min_price} in {args.db}")
        return

    rows = []
    for r in raw:
        fp = r["fill_price"]
        fee = buy_side_fee_cents(fp, r["signal_type"], r["series_ticker"])
        buy_pnl = r["resolved_price"] - fp - fee  # cents
        hold_days = max((r["resolved_at"] - r["filled_at"]) / 86400.0, MIN_HOLD_DAYS)
        rows.append({
            "fill_price": fp,
            "resolved_price": r["resolved_price"],
            "series_ticker": r["series_ticker"],
            "category": r["category"],
            "event_ticker": r["event_ticker"],
            "bucket_label": r["bucket_label"],
            "filled_at": r["filled_at"],
            "resolved_at": r["resolved_at"],
            "signal_type": r["signal_type"],
            "filter_version": r["filter_version"],
            "fee": fee,
            "buy_pnl": buy_pnl,
            "hold_days": hold_days,
            "sell_pnl_cents": r["sell_pnl_cents"],
        })

    overall = aggregate(rows)
    tail = tail_metrics(rows)
    top_cat, top_cat_concentration = category_concentration(rows)

    print(f"\n{'='*78}")
    print(f"BUY-FAVORITE BACKTEST  |  db={args.db}  |  fill_price >= {args.min_price}c")
    print(f"{'='*78}\n")

    # Sanity check vs SELL-side recorded P&L — only applies to original mispricing
    # rows (where the bot actually sold). Sports rows are pure observations with no
    # SELL side, so exclude them from this check.
    sell_rows = [r for r in rows if not (r["signal_type"] or "").startswith("sports_favorite_buy")]
    if sell_rows:
        sum_sell = sum(r["sell_pnl_cents"] for r in sell_rows if r["sell_pnl_cents"] is not None)
        sum_buy = sum(r["buy_pnl"] for r in sell_rows)
        sum_fees_2x = sum(r["fee"] * 2 for r in sell_rows)
        print(f"SANITY (sell-side rows only, n={len(sell_rows)}):")
        print(f"  sum(sell_pnl) = {fmt_money(sum_sell)} | sum(buy_pnl) = {fmt_money(sum_buy)}")
        print(f"  Expected: buy_pnl ~= -sell_pnl - 2*fee")
        print(f"  Implied:  -sell_pnl - 2*fee = {fmt_money(-sum_sell - sum_fees_2x)}")
        print(f"  Diff:     {fmt_money(sum_buy - (-sum_sell - sum_fees_2x))}  (should be near $0)\n")

    # Overall, with haircut scenarios
    print(f"=== OVERALL (n={overall['n']}) ===")
    print(f"  Win rate:                  {overall['win_rate']*100:.2f}%")
    print(f"  Avg hold:                  {overall['avg_hold_days']:.1f} days")
    print(f"  Capital-weighted APR:      {fmt_pct(overall['avg_apr_capital_weighted'])}  (haircut-invariant)")
    print(f"  Total deployed capital:    {fmt_money(overall['total_capital'])}  (gross, single-turn)")
    print(f"  Total dollar-days:         ${overall['total_dollar_days']/100:,.0f}")
    print(f"\n  P&L scenarios:")
    print(f"    Optimistic (100%):       {fmt_money(overall['total_pnl'])}")
    print(f"    Realistic ({int(args.realistic_haircut*100)}%):        {fmt_money(overall['total_pnl'] * args.realistic_haircut)}")
    print(f"    Pessimistic ({int(args.pessimistic_haircut*100)}%):       {fmt_money(overall['total_pnl'] * args.pessimistic_haircut)}")

    # Tail metrics
    print(f"\n=== TAIL METRICS (full sample, scale linearly with haircut) ===")
    print(f"  Worst single calendar day: {fmt_money(tail['worst_day_pnl'])} on {tail['worst_day']}")
    print(f"  Worst day % of capital:    {tail['worst_day_pct_of_capital']*100:.2f}%  (haircut-invariant)")
    print(f"  Max drawdown:              {fmt_money(-tail['max_drawdown'])}")
    print(f"  Longest losing streak:     {tail['longest_loss_streak']} trades")
    print(f"  Top category share of |P&L|: {top_cat} = {top_cat_concentration*100:.1f}%")

    # Stratification: by category
    print(f"\n=== BY CATEGORY ===")
    by_cat = defaultdict(list)
    for r in rows:
        by_cat[r["category"] or "?"].append(r)
    for cat, rs in sorted(by_cat.items(), key=lambda kv: -len(kv[1])):
        print_agg_line(cat, aggregate(rs))

    # By series (top 10)
    print(f"\n=== BY SERIES (top 10 by trade count) ===")
    by_series = defaultdict(list)
    for r in rows:
        by_series[r["series_ticker"]].append(r)
    top_series = sorted(by_series.items(), key=lambda kv: -len(kv[1]))[:10]
    for series, rs in top_series:
        print_agg_line(series, aggregate(rs))

    # By fill_price bucket
    print(f"\n=== BY FILL PRICE BUCKET ===")
    by_price = defaultdict(list)
    for r in rows:
        by_price[price_bucket(r["fill_price"])].append(r)
    for bucket in ["95-96", "97", "98", "99+"]:
        print_agg_line(bucket, aggregate(by_price.get(bucket, [])))

    # By filter version
    print(f"\n=== BY FILTER VERSION ===")
    by_fv = defaultdict(list)
    for r in rows:
        by_fv[r["filter_version"] or "?"].append(r)
    for fv, rs in sorted(by_fv.items()):
        print_agg_line(fv, aggregate(rs))

    # Decision rule
    print(f"\n{'='*78}")
    print(f"DECISION RULE (against realistic-haircut numbers)")
    print(f"{'='*78}")
    apr = overall["avg_apr_capital_weighted"]
    wr = overall["win_rate"]
    wd_pct = tail["worst_day_pct_of_capital"]
    cat_conc = top_cat_concentration

    apr_ok = apr > GO_APR_MIN
    wr_ok = wr > GO_WIN_RATE_MIN
    wd_ok = wd_pct < GO_WORST_DAY_PCT_MAX
    cat_ok = cat_conc < GO_CATEGORY_CONCENTRATION_MAX

    def mark(ok): return "PASS" if ok else "FAIL"
    print(f"  APR > {GO_APR_MIN*100:.0f}%:                   {fmt_pct(apr):>7s}    [{mark(apr_ok)}]")
    print(f"  Win rate > {GO_WIN_RATE_MIN*100:.0f}%:              {wr*100:5.2f}%    [{mark(wr_ok)}]")
    print(f"  Worst-day < {GO_WORST_DAY_PCT_MAX*100:.0f}% of capital: {wd_pct*100:5.2f}%    [{mark(wd_ok)}]")
    print(f"  Top category < {GO_CATEGORY_CONCENTRATION_MAX*100:.0f}% of |P&L|: {cat_conc*100:5.1f}%    [{mark(cat_ok)}]")

    decision = "GO" if all([apr_ok, wr_ok, wd_ok, cat_ok]) else "NO-GO"
    print(f"\n  >>> {decision} <<<")
    if decision == "NO-GO":
        failures = []
        if not apr_ok: failures.append("APR")
        if not wr_ok: failures.append("win-rate")
        if not wd_ok: failures.append("tail-risk")
        if not cat_ok: failures.append("category-concentration")
        print(f"  Failed: {', '.join(failures)}")
    print()


if __name__ == "__main__":
    main()
