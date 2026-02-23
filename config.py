ARB_THRESHOLD = 95          # Max combined price in cents to trigger arb
MAX_CONTRACTS = 25          # Contracts per leg
POLL_INTERVAL = 5           # Seconds between scan cycles
SERIES = ["KXBTC15M"]      # Series tickers to monitor
MAX_EXPOSURE = 50000_00     # Max deployed capital in cents ($50,000)
FILL_CHECK_DELAY = 5        # Seconds to wait before checking fill status
SAFETY_CANCEL_DELAY = 8     # Hard cancel deadline in seconds
