# mt5_connector.py

import MetaTrader5 as mt5
import pandas as pd
from utils import log_info, log_error

def initialize_mt5():
    if not mt5.initialize():
        log_error("MT5 initialization failed.")
        return False
    account = mt5.account_info()
    if account is None:
        log_error("Failed to get account info.")
        return False
    log_info(f"Connected to MT5 account {account.login}")
    return True

def shutdown_mt5():
    mt5.shutdown()
    log_info("Disconnected from MT5.")

def fetch_historical_data(symbol, timeframe, start_date, end_date):
    if not mt5.symbol_select(symbol, True):
        log_error(f"Symbol {symbol} not available in MT5.")
        return pd.DataFrame()

    utc_from = pd.to_datetime(start_date)
    utc_to = pd.to_datetime(end_date)

    rates = mt5.copy_rates_range(symbol, timeframe, utc_from, utc_to)
    if rates is None or len(rates) == 0:
        log_error(f"No data returned for {symbol} in given range.")
        return pd.DataFrame()

    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s")
    return df
