"""Market-maker configuration from environment variables."""

import os

MM_SERIES = os.getenv("MM_SERIES", "KXBTCD")
MM_HALF_SPREAD = int(os.getenv("MM_HALF_SPREAD", "5"))
MM_QUOTE_SIZE = int(os.getenv("MM_QUOTE_SIZE", "5"))
MM_MAX_INVENTORY = int(os.getenv("MM_MAX_INVENTORY", "5"))
MM_MAX_LOSS = int(os.getenv("MM_MAX_LOSS", "2500"))          # cents ($25)
MM_STRIKES = os.getenv("MM_STRIKES", "auto").lower()
MM_REQUOTE_INTERVAL = int(os.getenv("MM_REQUOTE_INTERVAL", "5"))
MM_MIN_EXPIRY = int(os.getenv("MM_MIN_EXPIRY", "3600"))      # 1 hour
MM_MAX_EXPIRY = int(os.getenv("MM_MAX_EXPIRY", "172800"))    # 48 hours
MM_CONFIRM = os.getenv("MM_CONFIRM", "false").lower() in ("true", "1", "yes")
MM_QUOTE_TOLERANCE = int(os.getenv("MM_QUOTE_TOLERANCE", "2"))
MM_MIN_BOOK_SPREAD = int(os.getenv("MM_MIN_BOOK_SPREAD", "3"))
MM_MAX_API_ERRORS = int(os.getenv("MM_MAX_API_ERRORS", "5"))

# --- Volatility-adaptive spread ---
MM_BASE_HALF_SPREAD = int(os.getenv("MM_BASE_HALF_SPREAD", str(MM_HALF_SPREAD)))  # minimum half spread (cents)
MM_VOL_MULTIPLIER = float(os.getenv("MM_VOL_MULTIPLIER", "2.0"))
MM_MAX_HALF_SPREAD = int(os.getenv("MM_MAX_HALF_SPREAD", "15"))
MM_VOL_WINDOW = int(os.getenv("MM_VOL_WINDOW", "60"))  # scans (60 * 5s = 5 min)

# --- Volatility pause ---
MM_VOL_PAUSE_THRESHOLD = int(os.getenv("MM_VOL_PAUSE_THRESHOLD", "200"))  # cents move in lookback
MM_VOL_PAUSE_LOOKBACK = int(os.getenv("MM_VOL_PAUSE_LOOKBACK", "12"))     # scans (12 * 5s = 60s)

# --- Smart requoting ---
MM_STALE_QUOTE_SECONDS = int(os.getenv("MM_STALE_QUOTE_SECONDS", "300"))  # 5 minutes
