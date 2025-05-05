import time
import os
import json
import pandas as pd
import MetaTrader5 as mt5

from config import *
from mt5_connector import (
    initialize_mt5, shutdown_mt5, fetch_historical_data,
    execute_trade, adjust_trailing_stop
)
from strategy import calculate_indicators
from funded_risk import DailyLossManager
from utils import log_info, log_error

# Load best config from file (symbol, timeframe, strategy params)
def load_best_config():
    try:
        with open("results/best_params.json", "r") as f:
            data = json.load(f)
            return data["symbol"], data["timeframe"], data["best_params"]
    except Exception as e:
        log_error(f"Failed to load best_params.json: {e}")
        return None, None, None

# Initialize MT5
if not initialize_mt5():
    log_error("Failed to initialize MT5. Exiting.")
    exit()

# Select symbol + timeframe + params
if USE_MANUAL_SYMBOL:
    symbol = MANUAL_SYMBOL
    timeframe = MANUAL_TIMEFRAME
    best_params = MANUAL_PARAMS
    log_info(f"[MANUAL MODE] Trading {symbol} on timeframe {timeframe}")
else:
    symbol, timeframe, best_params = load_best_config()
    if not all([symbol, timeframe, best_params]):
        log_error("Missing config from best_params.json. Exiting.")
        shutdown_mt5()
        exit()
    log_info(f"[AUTO MODE] Trading {symbol} on {timeframe} with loaded best params.")

# Setup daily loss logic
daily_loss_manager = DailyLossManager()

# Initial risk check
if FUNDED_MODE and daily_loss_manager.should_stop_bot():
    log_error("Max daily loss already hit. Stopping bot.")
    shutdown_mt5()
    exit()

# Main loop
while True:
    if os.path.exists("stop.flag"):
        log_info("Stop flag detected. Exiting.")
        os.remove("stop.flag")
        break

    if not mt5.initialize():
        log_error("Reinitializing MT5...")
        mt5.shutdown()
        time.sleep(5)
        continue

    if FUNDED_MODE:
        daily_loss_manager.update_day()
        if daily_loss_manager.should_stop_bot():
            log_error("FUNDED MODE: Max daily loss exceeded. Stopping.")
            shutdown_mt5()
            exit()

    df = fetch_historical_data(symbol, timeframe, Bars)
    if df.empty:
        log_error("No historical data.")
        time.sleep(TRADE_FREQUENCY_SECONDS)
        continue

    if best_params is None:
        log_error("No parameters defined for strategy.")
        shutdown_mt5()
        exit()

    df = calculate_indicators(df, best_params)

    last_row = df.iloc[-1]
    prev_row = df.iloc[-2]

    supertrend_signal = last_row['supertrend_signal'] if last_row['supertrend_signal'] == prev_row['supertrend_signal'] else "hold"
    adx = last_row['adx']
    rsi = last_row['rsi']
    price = last_row['close']

    log_info(f"SuperTrend: {supertrend_signal}, ADX: {adx}, RSI: {rsi}, Price: {price}")

    if all(pd.notna([supertrend_signal, adx, rsi, price])):
        if supertrend_signal == "buy" and adx >= best_params["adx_threshold"] and best_params["rsi_oversold"] <= rsi <= best_params["rsi_overbought"]:
            execute_trade(symbol, "buy", price)
        elif supertrend_signal == "sell" and adx >= best_params["adx_threshold"] and best_params["rsi_oversold"] <= rsi <= best_params["rsi_overbought"]:
            execute_trade(symbol, "sell", price)

    adjust_trailing_stop()
    time.sleep(TRADE_FREQUENCY_SECONDS)
