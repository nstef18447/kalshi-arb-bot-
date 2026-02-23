import os
import sys
import logging

from dotenv import load_dotenv

import config
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
    print(f"\n{'='*50}")
    print(f"  Kalshi Arb Bot — {env} MODE")
    print(f"{'='*50}")
    print(f"  Series:          {config.SERIES}")
    print(f"  Arb threshold:   {config.ARB_THRESHOLD} cents")
    print(f"  Contracts/leg:   {config.MAX_CONTRACTS}")
    print(f"  Poll interval:   {config.POLL_INTERVAL}s")
    print(f"  Max exposure:    ${config.MAX_EXPOSURE / 100:,.2f}")
    print(f"  Fill check:      {config.FILL_CHECK_DELAY}s")
    print(f"  Safety cancel:   {config.SAFETY_CANCEL_DELAY}s")
    print(f"{'='*50}\n")


def main():
    load_dotenv()
    setup_logging()
    validate_env()
    print_config()

    try:
        balance = get_balance()
        print(f"  Account balance: ${balance / 100:,.2f}\n")
    except Exception as e:
        print(f"  Could not fetch balance: {e}\n")
        print("  Check your API credentials and try again.")
        sys.exit(1)

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
