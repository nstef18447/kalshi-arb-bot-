"""Market-maker configuration from environment variables."""

import os

MM_SERIES = os.getenv("MM_SERIES", "KXBTCD")
MM_SERIES_LIST = [s.strip() for s in os.getenv("MM_SERIES_LIST", MM_SERIES).split(",")]
MM_POLL_OVERRIDES = {"KXBTCD": 5, "KXBTC15M": 1}  # seconds between poll cycles per series
MM_HALF_SPREAD = int(os.getenv("MM_HALF_SPREAD", "5"))
MM_QUOTE_SIZE = int(os.getenv("MM_QUOTE_SIZE", "5"))
MM_MAX_INVENTORY = int(os.getenv("MM_MAX_INVENTORY", "5"))
MM_MAX_LOSS = int(os.getenv("MM_MAX_LOSS", "2500"))          # cents ($25)
MM_STRIKES = os.getenv("MM_STRIKES", "auto").lower()
MM_REQUOTE_INTERVAL = int(os.getenv("MM_REQUOTE_INTERVAL", "5"))
MM_MIN_EXPIRY = int(os.getenv("MM_MIN_EXPIRY", "600"))        # 10 minutes (hourly events close fast)
MM_HOURLY_MAX_TTL = int(os.getenv("MM_HOURLY_MAX_TTL", "7200"))    # 2h — events shorter than this are "hourly"
MM_DAILY_MAX_TTL = int(os.getenv("MM_DAILY_MAX_TTL", "93600"))     # 26h — events shorter than this are "daily"
MM_QUOTE_WEEKLY = os.getenv("MM_QUOTE_WEEKLY", "false").lower() in ("true", "1", "yes")
MM_CONFIRM = os.getenv("MM_CONFIRM", "false").lower() in ("true", "1", "yes")
MM_QUOTE_TOLERANCE = int(os.getenv("MM_QUOTE_TOLERANCE", "2"))
MM_MIN_BOOK_SPREAD = int(os.getenv("MM_MIN_BOOK_SPREAD", "3"))
MM_MAX_API_ERRORS = int(os.getenv("MM_MAX_API_ERRORS", "5"))

# --- Volatility-adaptive spread ---
MM_BASE_HALF_SPREAD = int(os.getenv("MM_BASE_HALF_SPREAD", str(MM_HALF_SPREAD)))  # minimum half spread (cents)
MM_VOL_MULTIPLIER = float(os.getenv("MM_VOL_MULTIPLIER", "2.0"))
MM_MAX_HALF_SPREAD = int(os.getenv("MM_MAX_HALF_SPREAD", "15"))
MM_VOL_WINDOW = int(os.getenv("MM_VOL_WINDOW", "60"))  # scans (60 * 5s = 5 min)
MM_VOL_EMA_ALPHA = float(os.getenv("MM_VOL_EMA_ALPHA", "0.3"))  # EMA smoothing factor

# --- Volatility pause (dual trigger) ---
MM_VOL_PAUSE_THRESHOLD = float(os.getenv("MM_VOL_PAUSE_THRESHOLD", "0.003"))  # 0.3% ATM strike move
MM_MID_MOVE_PAUSE = int(os.getenv("MM_MID_MOVE_PAUSE", "15"))  # cents contract mid move in lookback
MM_VOL_PAUSE_LOOKBACK = int(os.getenv("MM_VOL_PAUSE_LOOKBACK", "12"))  # scans (12 * 5s = 60s)

# --- Smart requoting ---
MM_STALE_QUOTE_SECONDS = int(os.getenv("MM_STALE_QUOTE_SECONDS", "300"))  # 5 minutes
MM_COMPETITIVENESS_CHECK_AGE = int(os.getenv("MM_COMPETITIVENESS_CHECK_AGE", "30"))  # seconds
