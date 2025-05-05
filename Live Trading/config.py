import MetaTrader5 as mt5

# MetaTrader 5 account credentials
MT5_ACCOUNT = 5035498531
MT5_PASSWORD = "WhZcQg!4"
MT5_SERVER = "MetaQuotes-Demo"

# Trading Settings
LOT_SIZE = 0.3
Bars = 10000
TRAILING_STOP_TRIGGER_PIPS = 50
TRAILING_STOP_ENABLED = True
TRAILING_STOP_DISTANCE_PIPS = 30
TRADE_FREQUENCY_SECONDS = 30

# Symbol & Timeframe Settings
USE_MANUAL_SYMBOL = False
MANUAL_SYMBOL = "EURUSD"
MANUAL_TIMEFRAME = mt5.TIMEFRAME_M5
MANUAL_PARAMS = {
    "supertrend_period": 10,
    "supertrend_multiplier": 3,
    "adx_period": 14,
    "adx_threshold": 25,
    "rsi_period": 14,
    "rsi_oversold": 30,
    "rsi_overbought": 70
}


# Prop firm logic
FUNDED_MODE = True
MAX_DAILY_DRAWDOWN_PERCENT = 4.5  # If needed in future
