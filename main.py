import os
import signal
import sys
import logging
import threading

from dotenv import load_dotenv

# Load .env BEFORE importing config so MODE/READ_ONLY are set correctly
load_dotenv()

import config
import db_logger
from bot import ArbBot
from kalshi_api import get_balance


def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def validate_env():
    required = ["KALSHI_API_KEY", "KALSHI_PRIVATE_KEY_PATH"]
    missing = [k for k in required if not os.getenv(k)]
    if missing:
        print(f"Missing required env vars: {', '.join(missing)}")
        print("Copy .env.example to .env and fill in your credentials.")
        sys.exit(1)

    key_path = os.getenv("KALSHI_PRIVATE_KEY_PATH", "")
    if not os.path.exists(key_path):
        print(f"Private key file not found: {key_path}")
        sys.exit(1)


def print_config():
    env = os.getenv("KALSHI_ENV", "demo").upper()

    if config.MODE == "market_maker":
        import mm_config as mc
        print(f"\n{'='*50}")
        print(f"  Kalshi Market Maker — {env}")
        print(f"{'='*50}")
        for s in mc.MM_SERIES_LIST:
            poll = mc.MM_POLL_OVERRIDES.get(s, mc.MM_REQUOTE_INTERVAL)
            ctype = config.SERIES.get(s, {}).get("contract_type", "?")
            print(f"  Series:          {s} ({ctype}, poll={poll}s)")
        for s in mc.MM_SERIES_LIST:
            hs = mc.MM_BASE_HALF_SPREAD_OVERRIDES.get(s, mc.MM_BASE_HALF_SPREAD)
            print(f"  Half spread {s}: {hs}c (spread={hs*2}c)")
        print(f"  Dynamic range:   {mc.MM_BASE_HALF_SPREAD*2}-{mc.MM_MAX_HALF_SPREAD*2}c")
        print(f"  Vol multiplier:  {mc.MM_VOL_MULTIPLIER}x (EMA alpha={mc.MM_VOL_EMA_ALPHA})")
        print(f"  Vol window:      {mc.MM_VOL_WINDOW} scans ({mc.MM_VOL_WINDOW * mc.MM_REQUOTE_INTERVAL}s)")
        print(f"  Vol pause:       {mc.MM_VOL_PAUSE_THRESHOLD:.1%} ATM strike move OR {mc.MM_MID_MOVE_PAUSE}c mid move in {mc.MM_VOL_PAUSE_LOOKBACK * mc.MM_REQUOTE_INTERVAL}s")
        print(f"  Competitive age: {mc.MM_COMPETITIVENESS_CHECK_AGE}s")
        print(f"  Quote size:      {mc.MM_QUOTE_SIZE}")
        print(f"  Max inventory:   {mc.MM_MAX_INVENTORY}")
        print(f"  Max loss:        {mc.MM_MAX_LOSS}c (${mc.MM_MAX_LOSS/100:.2f})")
        print(f"  Strikes:         {mc.MM_STRIKES}")
        print(f"  Requote interval:{mc.MM_REQUOTE_INTERVAL}s")
        print(f"  Stale quote:     {mc.MM_STALE_QUOTE_SECONDS}s")
        print(f"  Min TTL:         {mc.MM_MIN_EXPIRY}s")
        print(f"  Tier cutoffs:    hourly <{mc.MM_HOURLY_MAX_TTL/3600:.0f}h, daily <{mc.MM_DAILY_MAX_TTL/3600:.0f}h, weekly >={mc.MM_DAILY_MAX_TTL/3600:.0f}h")
        print(f"  Quote weekly:    {mc.MM_QUOTE_WEEKLY}")
        print(f"  Quote tolerance: {mc.MM_QUOTE_TOLERANCE}c")
        print(f"  Min book spread: {mc.MM_MIN_BOOK_SPREAD}c")
        print(f"  Max API errors:  {mc.MM_MAX_API_ERRORS}")
        print(f"  TTL cutoff:      {mc.MM_MIN_TTL_SECONDS}s (binary end)")
        print(f"  Window buffer:   {mc.MM_MIN_TTL_START_SECONDS}s (binary start)")
        print(f"  BTC spot pause:  {mc.MM_BTC_SPOT_MOVE_PCT:.2%} in {mc.MM_BTC_SPOT_LOOKBACK}s → {mc.MM_BTC_SPOT_PAUSE}s pause")
        print(f"  One-sided fills: {mc.MM_ONESIDED_FILL_LIMIT} consecutive → {mc.MM_ONESIDED_PAUSE}s pause")
        if mc.MM_CONFIRM:
            print(f"\n  MODE: LIVE QUOTING")
        else:
            print(f"\n  MODE: DRY RUN (no orders placed)")
        print(f"{'='*50}\n")
        return

    if config.MODE == "mispricing_scanner":
        from mispricing_scanner import MULTI_OUTCOME_SERIES, OVERPRICING_THRESHOLD_CENTS, MIN_TOTAL_EXCESS_CENTS
        print(f"\n{'='*50}")
        print(f"  Kalshi Mispricing Scanner — {env}")
        print(f"{'='*50}")
        print(f"  Series:          {list(MULTI_OUTCOME_SERIES.keys())}")
        print(f"  Categories:      {sorted(set(s['category'] for s in MULTI_OUTCOME_SERIES.values()))}")
        print(f"  Overpricing gap: {OVERPRICING_THRESHOLD_CENTS}c (flag if YES price > fair value + {OVERPRICING_THRESHOLD_CENTS}c)")
        print(f"  Min event excess:{MIN_TOTAL_EXCESS_CENTS}c (only scan if total YES > {100 + MIN_TOTAL_EXCESS_CENTS}c)")
        print(f"\n  MODE: READ-ONLY (signal logging only, no trading)")
        print(f"{'='*50}\n")
        return

    if config.MODE == "binary_arb":
        print(f"\n{'='*50}")
        print(f"  Kalshi Binary Arb Bot — {env}")
        print(f"{'='*50}")
        print(f"  Series:          KXBTC15M")
        print(f"  Threshold:       {config.BINARY_ARB_THRESHOLD} cents (yes+no combined)")
        print(f"  Size:            {config.BINARY_ARB_SIZE} contracts")
        print(f"  Cooldown:        {config.BINARY_ARB_COOLDOWN}s per ticker")
        print(f"  Hedge delay:     {config.BINARY_ARB_HEDGE_DELAY}s")
        print(f"  Poll interval:   {config.POLL_INTERVAL}s")
        if config.READ_ONLY:
            print(f"\n  MODE: READ-ONLY (scan only, no trading)")
        else:
            print(f"\n  MODE: LIVE TRADING")
        print(f"{'='*50}\n")
        return

    print(f"\n{'='*50}")
    print(f"  Kalshi Arb Bot — {env} MODE")
    print(f"{'='*50}")
    print(f"  Series:          {list(config.SERIES.keys())}")
    for ticker, fees in config.SERIES.items():
        print(f"    {ticker:12s} taker={fees['taker_fee']:.1%}  maker_mult={fees['maker_mult']}")
    print(f"  Arb threshold:   {config.ARB_THRESHOLD} cents")
    print(f"  Contracts/leg:   {config.MAX_CONTRACTS}")
    print(f"  Poll interval:   {config.POLL_INTERVAL}s")
    print(f"  Max exposure:    ${config.MAX_EXPOSURE / 100:,.2f}")
    print(f"  Min depth:       {config.MIN_DEPTH} contracts")
    print(f"  1st leg timeout: {config.FIRST_LEG_TIMEOUT}s")
    print(f"  2nd leg timeout: {config.SECOND_LEG_TIMEOUT}s")
    print(f"  Circuit breaker: {config.MAX_ORPHAN_RATE:.0%} orphan rate -> {config.COOLDOWN_MINUTES}min pause")
    print(f"  Snapshot cache:  {config.SNAPSHOT_CACHE_SIZE} ({config.SNAPSHOT_CACHE_SIZE * config.POLL_INTERVAL}s history)")
    print(f"  Soft arb prob:   {config.SOFT_ARB_PROB_THRESHOLD:.0%}")
    if config.READ_ONLY:
        print(f"\n  MODE: READ-ONLY (scan only, no trading)")
    else:
        print(f"\n  MODE: LIVE TRADING")
    print(f"{'='*50}\n")


def main():
    setup_logging()
    validate_env()
    print_config()

    # Initialize analytics database
    try:
        db_logger.init_db()
        print("  Database initialized (arb_bot.db)")
    except Exception as e:
        print(f"  WARNING: Could not initialize database: {e}")
        print("  Bot will run but dashboard data won't be collected.")

    try:
        balance = get_balance()
        print(f"  Account balance: ${balance / 100:,.2f}\n")
    except Exception as e:
        print(f"  Could not fetch balance: {e}\n")
        if config.READ_ONLY:
            print("  Continuing in READ_ONLY mode (balance not required).\n")
        else:
            print("  Check your API credentials and try again.")
            sys.exit(1)

    if config.MODE == "mispricing_scanner":
        from mispricing_scanner import MispricingScanner
        scanner = MispricingScanner()
        try:
            scanner.start()
        except KeyboardInterrupt:
            pass
        finally:
            scanner.stop()
            print("\nMispricing scanner shut down cleanly.")
    elif config.MODE == "market_maker":
        import mm_config as mc
        from mm_engine import MarketMaker

        series_list = mc.MM_SERIES_LIST
        if len(series_list) == 1:
            # Single series — no threading, same as before
            mm = MarketMaker(series=series_list[0])
            try:
                mm.start()
            except KeyboardInterrupt:
                pass
            finally:
                mm.stop()
                print("\nMarket maker shut down cleanly.")
        else:
            # Multi-series — thread per series
            halt_event = threading.Event()
            instances = []
            threads = []
            for series in series_list:
                mm = MarketMaker(series=series, halt_event=halt_event)
                instances.append(mm)
                t = threading.Thread(target=mm.start, name=f"mm-{series}", daemon=True)
                threads.append(t)
                t.start()

            # Main thread: wait for halt or keyboard interrupt
            signal.signal(signal.SIGINT, lambda s, f: halt_event.set())
            signal.signal(signal.SIGTERM, lambda s, f: halt_event.set())
            try:
                halt_event.wait()
            except KeyboardInterrupt:
                halt_event.set()

            for mm in instances:
                mm.running = False
            for t in threads:
                t.join(timeout=10)
            print("\nAll market makers shut down cleanly.")
    elif config.MODE == "binary_arb":
        from binary_arb_bot import BinaryArbBot
        bot = BinaryArbBot()
        try:
            bot.start()
        except KeyboardInterrupt:
            pass
        finally:
            bot.stop()
            print("\nBinary arb bot shut down cleanly.")
    else:
        bot = ArbBot()
        try:
            bot.start()
        except KeyboardInterrupt:
            pass
        finally:
            bot.stop()
            print("\nBot shut down cleanly.")


if __name__ == "__main__":
    main()
