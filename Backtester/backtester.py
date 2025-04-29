import MetaTrader5 as mt5
import pandas as pd
import os
import json
from datetime import datetime
from config import *
from utils import log_info, log_error
from mt5_connector import fetch_historical_data, initialize_mt5, shutdown_mt5
from strategy import calculate_indicators
from funded_risk import BacktestRiskManager

def is_session_allowed(current_time):
    for start, end in ALLOWED_SESSIONS:
        if start <= current_time <= end:
            return True
    return False

def backtest_supertrend(symbol, timeframe, start_date, end_date):
    best_params = {}
    max_profit = float('-inf')
    rejected_params = []
    total_params_tested = 0

    point = mt5.symbol_info(symbol).point

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

                                df = fetch_historical_data(symbol, timeframe, start_date, end_date)
                                if df.empty:
                                    continue

                                df = df[(df['time'] >= pd.to_datetime(start_date)) & (df['time'] <= pd.to_datetime(end_date))]
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
                                            log_error(f"Max total loss exceeded. Skipping params: {params}")
                                            rejected_params.append(params)
                                            balance = -999999
                                            break

                                        if risk_manager.is_daily_loss_exceeded(balance):
                                            continue  # Skip trading for rest of the day

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

                                if balance - START_BALANCE > max_profit:
                                    max_profit = balance - START_BALANCE
                                    best_params = params

    # --- Save results ---
    if not os.path.exists("results"):
        os.makedirs("results")

    with open("results/best_params.json", "w") as f:
        json.dump(best_params, f, indent=4)

    with open("results/rejected_params.json", "w") as f:
        json.dump(rejected_params, f, indent=4)

    summary = {
        "best_params": best_params,
        "best_profit": max_profit,
        "total_params_tested": total_params_tested,
        "total_rejected_params": len(rejected_params),
        "total_accepted_params": total_params_tested - len(rejected_params)
    }

    with open("results/final_backtest_summary.json", "w") as f:
        json.dump(summary, f, indent=4)

    log_info(f"Backtest completed: {summary}")

if __name__ == "__main__":
    if not initialize_mt5():
        log_error("MT5 initialization failed.")
        exit()

    for symbol in SYMBOL_LIST:
        try:
            backtest_supertrend(symbol, MANUAL_TIMEFRAME, BACKTEST_START_DATE, BACKTEST_END_DATE)
        except KeyboardInterrupt:
            log_info("Backtest interrupted by user.")
            shutdown_mt5()
            exit()

    shutdown_mt5()
