import MetaTrader5 as mt5
from datetime import time

# --- MT5 Credentials ---
MT5_ACCOUNT = 5035498531
MT5_PASSWORD = "WhZcQg!4"
MT5_SERVER = "MetaQuotes-Demo"

# --- Trading Settings ---
LOT_SIZE = 0.9
TRAILING_STOP_TRIGGER_PIPS = 50
TRAILING_STOP_DISTANCE_PIPS = 30
spread_pips = 1.5      # Simulated spread
commission_per_trade = -7.0  # Total round-trip commission


# --- Risk Management ---
START_BALANCE = 10000  # Starting balance in USD
DAILY_MAX_LOSS_PERCENT = 4.5  # Daily loss limit (% of start balance)
MAX_TOTAL_LOSS_PERCENT = 10.0  # Max loss allowed on total balance (% of start balance)

# --- Trading Behavior ---
FUNDED_MODE =True # Enable/disable daily/max loss protection

# --- Backtest Settings ---
TIMEFRAME_LIST = [mt5.TIMEFRAME_M1, mt5.TIMEFRAME_M5, mt5.TIMEFRAME_M15]
SYMBOL_LIST = ["EURUSD"]

BACKTEST_START_DATE = "2025-05-05"
BACKTEST_END_DATE = "2025-05-9"

ALLOWED_SESSIONS = [
    (time(7, 0), time(11, 59)),  # London session
    (time(13, 0), time(17, 0))   # New York session
]

WEEKEND_DAYS = [5, 6]  # Saturday and Sunday (skip trading)
