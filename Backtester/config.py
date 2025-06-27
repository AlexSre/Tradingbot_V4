import MetaTrader5 as mt5
from datetime import time

# --- MT5 Credentials ---
MT5_ACCOUNT = 52333432
MT5_PASSWORD = "0hi!TwIOaL&BYd"
MT5_SERVER = "ICMarketsEU-Demo"

# --- Trading Settings ---
LOT_SIZE = 0.9
TRAILING_STOP_TRIGGER_PIPS = 100
TRAILING_STOP_DISTANCE_PIPS = 60
spread_pips = 0.0    # Simulated spread
commission_per_trade = 0.0  # Total round-trip commission
SLIPPAGE_PIPS = 0.5



# --- Risk Management ---
START_BALANCE = 10000  # Starting balance in USD
DAILY_MAX_LOSS_PERCENT = 4.5  # Daily loss limit (% of start balance)
MAX_TOTAL_LOSS_PERCENT = 10.0  # Max loss allowed on total balance (% of start balance)

# --- Trading Behavior ---
FUNDED_MODE =True# Enable/disable daily/max loss protection

# --- Backtest Settings ---
TIMEFRAME_LIST = [mt5.TIMEFRAME_M15]
SYMBOL_LIST = ["US30"]

BACKTEST_START_DATE = "2025-05-19"
BACKTEST_END_DATE = "2025-05-20"

ALLOWED_SESSIONS = [
    (time(7, 0), time(12, 59)),  # London session
    (time(13, 0), time(17, 0))   # New York session
]

WEEKEND_DAYS = [5, 6]  # Saturday and Sunday (skip trading)
