import MetaTrader5 as mt5
import pandas as pd
import time
from config import MT5_ACCOUNT, MT5_PASSWORD, MT5_SERVER
from utils import log_info, log_error

def initialize_mt5():
    """
    Start the MT5 terminal (if not already running) and log in.
    Returns True on success, False otherwise.
    """
    # initialize the terminal
    if not mt5.initialize():
        log_error("MT5 initialize() call failed.")
        return False

    # attempt login
    if not mt5.login(MT5_ACCOUNT, MT5_PASSWORD, MT5_SERVER):
        log_error(f"MT5 login() failed for account {MT5_ACCOUNT} on {MT5_SERVER}.")
        mt5.shutdown()
        return False

    account_info = mt5.account_info()
    if account_info is None:
        log_error("MT5.account_info() returned None after login.")
        mt5.shutdown()
        return False

    log_info(f"Connected to MT5 account {account_info.login} (Balance: {account_info.balance})")
    return True

def shutdown_mt5():
    """Cleanly disconnect from MT5."""
    mt5.shutdown()
    log_info("MT5 connection closed")

def fetch_historical_data(symbol, timeframe, start, end=None):
    """
    Fetch a range of bars for `symbol` between `start` and `end` datetimes.
    If `end` is None, fetches from `start` up to now.
    """
    if not mt5.symbol_select(symbol, True):
        log_error(f"Symbol {symbol} not available in MT5.")
        return pd.DataFrame()

    utc_from = pd.to_datetime(start)
    utc_to = pd.to_datetime(end) if end is not None else pd.Timestamp.utcnow()

    rates = mt5.copy_rates_range(symbol, timeframe, utc_from, utc_to)
    if rates is None or len(rates) == 0:
        log_error(f"No data returned for {symbol} in given range.")
        return pd.DataFrame()

    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s")
    return df
