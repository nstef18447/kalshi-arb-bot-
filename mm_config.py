"""Market-maker configuration from environment variables."""

import os

MM_SERIES = os.getenv("MM_SERIES", "KXBTCD")
MM_HALF_SPREAD = int(os.getenv("MM_HALF_SPREAD", "5"))
MM_QUOTE_SIZE = int(os.getenv("MM_QUOTE_SIZE", "5"))
MM_MAX_INVENTORY = int(os.getenv("MM_MAX_INVENTORY", "5"))
MM_MAX_LOSS = int(os.getenv("MM_MAX_LOSS", "5000"))          # cents ($50)
MM_STRIKES = os.getenv("MM_STRIKES", "auto").lower()
MM_REQUOTE_INTERVAL = int(os.getenv("MM_REQUOTE_INTERVAL", "5"))
MM_MIN_EXPIRY = int(os.getenv("MM_MIN_EXPIRY", "3600"))      # 1 hour
MM_MAX_EXPIRY = int(os.getenv("MM_MAX_EXPIRY", "172800"))    # 48 hours
MM_CONFIRM = os.getenv("MM_CONFIRM", "false").lower() in ("true", "1", "yes")
MM_QUOTE_TOLERANCE = int(os.getenv("MM_QUOTE_TOLERANCE", "2"))
MM_MIN_BOOK_SPREAD = int(os.getenv("MM_MIN_BOOK_SPREAD", "3"))
MM_MAX_API_ERRORS = int(os.getenv("MM_MAX_API_ERRORS", "5"))
