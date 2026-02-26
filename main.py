import os
import sys
import logging

from dotenv import load_dotenv

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
        print(f"  Series:          {mc.MM_SERIES}")
        print(f"  Half spread:     {mc.MM_HALF_SPREAD}c")
        print(f"  Quote size:      {mc.MM_QUOTE_SIZE}")
        print(f"  Max inventory:   {mc.MM_MAX_INVENTORY}")
        print(f"  Max loss:        {mc.MM_MAX_LOSS}c (${mc.MM_MAX_LOSS/100:.2f})")
        print(f"  Strikes:         {mc.MM_STRIKES}")
        print(f"  Requote interval:{mc.MM_REQUOTE_INTERVAL}s")
        print(f"  TTL window:      {mc.MM_MIN_EXPIRY}s — {mc.MM_MAX_EXPIRY}s")
        print(f"  Quote tolerance: {mc.MM_QUOTE_TOLERANCE}c")
        print(f"  Min book spread: {mc.MM_MIN_BOOK_SPREAD}c")
        print(f"  Max API errors:  {mc.MM_MAX_API_ERRORS}")
        if mc.MM_CONFIRM:
            print(f"\n  MODE: LIVE QUOTING")
        else:
            print(f"\n  MODE: DRY RUN (no orders placed)")
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
    load_dotenv()
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
        print("  Check your API credentials and try again.")
        sys.exit(1)

    if config.MODE == "market_maker":
        from mm_engine import MarketMaker
        mm = MarketMaker()
        try:
            mm.start()
        except KeyboardInterrupt:
            pass
        finally:
            mm.stop()
            print("\nMarket maker shut down cleanly.")
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
