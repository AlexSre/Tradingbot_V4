import time
import pandas as pd
import os
import MetaTrader5 as mt5
from mt5_connector import (
    initialize_mt5, shutdown_mt5, get_open_chart, fetch_historical_data,
    execute_trade, adjust_trailing_stop
)
from strategy import calculate_indicators
from config import Bars, TRADE_FREQUENCY_SECONDS, USE_MT5_CHART, MANUAL_SYMBOL, MANUAL_TIMEFRAME, FUNDED_MODE, MAX_DAILY_DRAWDOWN_PERCENT
from utils import log_info, log_error, DailyLossManager
import json
from funded_risk import DailyLossManager


# Initialize MT5
if not initialize_mt5():
    log_error("Failed to initialize MT5. Exiting.")
    exit()

# Detect or set Symbol & Timeframe
if USE_MT5_CHART:
    result = get_open_chart()
    if result and result[0]:
        symbol, timeframe = result
        if isinstance(timeframe, str):
            timeframe = getattr(mt5, f"TIMEFRAME_{timeframe.upper()}", mt5.TIMEFRAME_M5)
    else:
        log_error("Failed to get chart details from MT5. Using manual settings.")
        symbol, timeframe = MANUAL_SYMBOL, MANUAL_TIMEFRAME
else:
    symbol, timeframe = MANUAL_SYMBOL, MANUAL_TIMEFRAME

if isinstance(timeframe, str):
    timeframe = getattr(mt5, f"TIMEFRAME_{timeframe.upper()}", mt5.TIMEFRAME_M5)

log_info(f"Trading on {symbol} ({timeframe})")

# Load best parameters from JSON
try:
    with open("../backtester/results/best_params.json") as f:
        best_params = json.load(f)
        log_info(f"Loaded best parameters: {best_params}")
except Exception as e:
    log_error(f"Failed to load best parameters: {e}")
    exit()


account_info = mt5.account_info()
daily_loss_manager = DailyLossManager()


if FUNDED_MODE and daily_loss_manager.should_stop_bot():
    log_error("Daily loss limit exceeded. Stopping bot immediately.")
    shutdown_mt5()
    exit()

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
        daily_loss_manager.update()

        if daily_loss_manager.should_stop_bot():
            log_error("Maximum daily loss exceeded. Stopping bot immediately (FUNDED MODE).")
            shutdown_mt5()
            exit()

    df = fetch_historical_data(symbol, timeframe, Bars)
    if df.empty:
        log_error("No historical data retrieved.")
        time.sleep(TRADE_FREQUENCY_SECONDS)
        continue

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
