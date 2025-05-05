import MetaTrader5 as mt5
import pandas as pd
import os
import json
import multiprocessing
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime
from config import (
    START_BALANCE, BACKTEST_START_DATE, BACKTEST_END_DATE,
    SYMBOL_LIST, TIMEFRAME_LIST, FUNDED_MODE, WEEKEND_DAYS, ALLOWED_SESSIONS
)
from utils import log_info, log_error
from mt5_connector import fetch_historical_data, initialize_mt5, shutdown_mt5
from strategy import calculate_indicators
from funded_risk import BacktestRiskManager

def is_session_allowed(current_time):
    for start, end in ALLOWED_SESSIONS:
        if start <= current_time <= end:
            return True
    return False

def backtest_combo(args):
    symbol, timeframe = args
    best_params = {}
    max_profit = float('-inf')
    rejected_params = []
    total_params_tested = 0
    if not mt5.symbol_select(symbol, True):
        log_error(f"Failed to select symbol {symbol}")
        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "best_params": {},
            "best_profit": -999999,
            "rejected_count": 0,
            "total_tested": 0
        }

    info = mt5.symbol_info(symbol)
    if info is None:
        log_error(f"Symbol info not found for {symbol}")
        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "best_params": {},
            "best_profit": -999999,
            "rejected_count": 0,
            "total_tested": 0
        }

    point = info.point

    for atr_period in range(5, 15):
        for multiplier in range(2, 6):
            for adx_period in range(10, 20, 5):
                for adx_threshold in range(20, 35, 5):
                    for rsi_period in range(10, 20, 5):
                        for rsi_low in range(25, 40, 5):
                            for rsi_high in range(60, 75, 5):
                                total_params_tested += 1

                                params = {
                                    "supertrend_period": atr_period,
                                    "supertrend_multiplier": multiplier,
                                    "adx_period": adx_period,
                                    "adx_threshold": adx_threshold,
                                    "rsi_period": rsi_period,
                                    "rsi_oversold": rsi_low,
                                    "rsi_overbought": rsi_high
                                }

                                df = fetch_historical_data(symbol, timeframe, BACKTEST_START_DATE, BACKTEST_END_DATE)
                                if df.empty:
                                    continue

                                df = df[(df['time'] >= pd.to_datetime(BACKTEST_START_DATE)) & (df['time'] <= pd.to_datetime(BACKTEST_END_DATE))]
                                df = calculate_indicators(df, params)

                                balance = START_BALANCE
                                position = None
                                entry_price = 0
                                stop_loss = 0
                                risk_manager = BacktestRiskManager()

                                for i in range(2, len(df)):
                                    row = df.iloc[i]
                                    prev_row = df.iloc[i - 1]

                                    risk_manager.update_day(row.name, balance)

                                    if FUNDED_MODE:
                                        if risk_manager.is_max_total_loss_exceeded(balance):
                                            rejected_params.append(params)
                                            balance = -999999
                                            break
                                        if risk_manager.is_daily_loss_exceeded(balance):
                                            continue

                                    if row.name.weekday() in WEEKEND_DAYS:
                                        continue

                                    if not all(pd.notna([row['supertrend_signal'], row['adx'], row['rsi'], row['close']])):
                                        continue

                                    if not is_session_allowed(row.name.time()):
                                        continue

                                    current = row['supertrend_signal']
                                    previous = prev_row['supertrend_signal']

                                    if current == previous:
                                        signal = current
                                        adx = row['adx']
                                        rsi = row['rsi']
                                        close_price = row['close']

                                        if adx >= params["adx_threshold"] and params["rsi_oversold"] <= rsi <= params["rsi_overbought"]:
                                            if signal == "buy" and position != "buy":
                                                if position == "sell":
                                                    balance += (entry_price - close_price) * (LOT_SIZE / point)
                                                position = "buy"
                                                entry_price = close_price
                                                stop_loss = entry_price - (TRAILING_STOP_DISTANCE_PIPS * point)

                                            elif signal == "sell" and position != "sell":
                                                if position == "buy":
                                                    balance += (close_price - entry_price) * (LOT_SIZE / point)
                                                position = "sell"
                                                entry_price = close_price
                                                stop_loss = entry_price + (TRAILING_STOP_DISTANCE_PIPS * point)

                                    if position == "buy":
                                        if close_price - stop_loss >= TRAILING_STOP_TRIGGER_PIPS * point:
                                            stop_loss = max(stop_loss, close_price - (TRAILING_STOP_DISTANCE_PIPS * point))
                                        if close_price <= stop_loss:
                                            balance += (stop_loss - entry_price) * (LOT_SIZE / point)
                                            position = None

                                    elif position == "sell":
                                        if stop_loss - close_price >= TRAILING_STOP_TRIGGER_PIPS * point:
                                            stop_loss = min(stop_loss, close_price + (TRAILING_STOP_DISTANCE_PIPS * point))
                                        if close_price >= stop_loss:
                                            balance += (entry_price - stop_loss) * (LOT_SIZE / point)
                                            position = None

                                profit = float(balance - START_BALANCE)
                                if profit > max_profit:
                                    max_profit = profit
                                    best_params = params

    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "best_params": best_params,
        "best_profit": float(max_profit),
        "rejected_count": len(rejected_params),
        "total_tested": total_params_tested
    }

if __name__ == "__main__":
    if not initialize_mt5():
        log_error("MT5 initialization failed.")
        exit()

    task_list = [(symbol, tf) for symbol in SYMBOL_LIST for tf in TIMEFRAME_LIST]

    log_info(f"Starting parallel backtest on {len(task_list)} tasks using {multiprocessing.cpu_count()} cores...")

    with ProcessPoolExecutor(max_workers=multiprocessing.cpu_count()) as executor:
        results = list(executor.map(backtest_combo, task_list))

    best = max(results, key=lambda r: r["best_profit"])

    if not os.path.exists("results"):
        os.makedirs("results")

    with open("results/best_params.json", "w") as f:
        json.dump(best, f, indent=4)

    log_info(f"Best backtest result saved: {best}")
    shutdown_mt5()
