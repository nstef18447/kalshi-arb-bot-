import os

# --- Mode ---
READ_ONLY = os.getenv("READ_ONLY", "true").lower() in ("true", "1", "yes")

ARB_THRESHOLD = 95          # Max combined price in cents to trigger arb
MAX_CONTRACTS = 25          # Contracts per leg
POLL_INTERVAL = 5           # Seconds between scan cycles
MAX_EXPOSURE = 50000_00     # Max deployed capital in cents ($50,000)

# --- Series to monitor (ticker -> fee config) ---
# taker_fee: flat rate applied to payout (e.g. 0.07 = 7%)
# maker_mult: multiplier in maker fee formula: mult * P * (1-P)
SERIES = {
    "KXBTC":     {"taker_fee": 0.07,  "maker_mult": 0.0175},
    "KXINX":       {"taker_fee": 0.035, "maker_mult": 0.00875},
    "KXNASDAQ100": {"taker_fee": 0.035, "maker_mult": 0.00875},
}

# --- Execution tuning ---
MIN_DEPTH = 30              # Min contracts available at best ask on both sides
FIRST_LEG_TIMEOUT = 2       # Seconds to wait for first (illiquid) leg fill
SECOND_LEG_TIMEOUT = 3      # Seconds to wait for second leg fill

# --- Circuit breaker ---
WINDOW_SIZE = 20            # Rolling window of attempts for orphan rate calc
MAX_ORPHAN_RATE = 0.25      # Pause bot if orphan rate exceeds this
COOLDOWN_MINUTES = 10       # Minutes to pause when circuit breaker trips

# --- Ladder scanner ---
SNAPSHOT_CACHE_SIZE = 12        # Last N snapshots per window (12 × 5s = 60s)
SOFT_ARB_PROB_THRESHOLD = 0.60  # Min range probability for soft arb flag
