import os

# --- Mode ---
MODE = os.getenv("MODE", "read_only").lower()
READ_ONLY = MODE not in ("arb", "market_maker")

ARB_THRESHOLD = 95          # Max combined price in cents to trigger arb
MAX_CONTRACTS = 25          # Contracts per leg
POLL_INTERVAL = 5           # Seconds between scan cycles
MAX_EXPOSURE = 50000_00     # Max deployed capital in cents ($50,000)

# --- Series to monitor (ticker -> fee config) ---
# taker_fee: flat rate applied to payout (e.g. 0.07 = 7%)
# maker_mult: multiplier in maker fee formula: mult * P * (1-P)
# contract_type: "above_below" = arb logic valid, "range" = monitoring only
SERIES = {
    # Above/below contracts — cross-strike arb logic is valid
    "KXBTCD":       {"taker_fee": 0.07,  "maker_mult": 0.0175, "poll_every": 1, "contract_type": "above_below"},
    "KXETHD":       {"taker_fee": 0.07,  "maker_mult": 0.0175, "poll_every": 1, "contract_type": "above_below"},
    "KXSOLD":       {"taker_fee": 0.07,  "maker_mult": 0.0175, "poll_every": 1, "contract_type": "above_below"},
    "KXINXU":       {"taker_fee": 0.035, "maker_mult": 0.00875, "poll_every": 6, "contract_type": "above_below"},
    "KXNASDAQ100U": {"taker_fee": 0.035, "maker_mult": 0.00875, "poll_every": 6, "contract_type": "above_below"},
    # Range contracts — arb logic does NOT apply, monitoring only
    "KXBTC":        {"taker_fee": 0.07,  "maker_mult": 0.0175, "poll_every": 6, "contract_type": "range"},
    "KXETH":        {"taker_fee": 0.07,  "maker_mult": 0.0175, "poll_every": 6, "contract_type": "range"},
    "KXSOLE":       {"taker_fee": 0.07,  "maker_mult": 0.0175, "poll_every": 6, "contract_type": "range"},
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
