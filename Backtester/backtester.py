import MetaTrader5 as mt5
import pandas as pd
import json
import os
from datetime import datetime
from multiprocessing import Pool, cpu_count
from itertools import product

from config import *
from utils import log_info, log_error
from mt5_connector import fetch_historical_data, initialize_mt5, shutdown_mt5
from strategy import calculate_indicators
from funded_risk import BacktestRiskManager

def worker_init():
    """Initialize MT5 in each pool worker."""
    if not mt5.initialize():
        log_error("MT5 initialization failed in worker")

def is_session_allowed(t: datetime.time) -> bool:
    for start, end in ALLOWED_SESSIONS:
        if start <= t <= end:
            return True
    return False

def simulate_params(task):
    """
    Simulate backtest for one set of params.
    task = (symbol, timeframe, params_dict, records_list)
    """
    symbol, timeframe, params, records = task

    # Build df
    df = pd.DataFrame.from_records(records)
    if 'time' not in df.columns:
        return {"profit": -float('inf'), "params": params}
    if not pd.api.types.is_datetime64_any_dtype(df['time']):
        df['time'] = pd.to_datetime(df['time'])
    df = calculate_indicators(df, params)
    if df.empty:
        return {"profit": -float('inf'), "params": params}

    # Ensure symbol info
    if not mt5.symbol_select(symbol, True):
        return {"profit": -float('inf'), "params": params}
    info = mt5.symbol_info(symbol)
    if info is None:
        return {"profit": -float('inf'), "params": params}

    point = info.point
    contract_size = info.trade_contract_size

    balance = START_BALANCE
    position = None
    entry = 0.0
    stop_loss = 0.0
    risk_mgr = BacktestRiskManager()

    for i in range(1, len(df)):
        row = df.iloc[i]
        prev = df.iloc[i - 1]

        # Daily / total loss checks
        risk_mgr.update_day(row.name, balance)
        if FUNDED_MODE:
            if risk_mgr.is_max_total_loss_exceeded(balance):
                balance = -float('inf')
                break
            if risk_mgr.is_daily_loss_exceeded(balance):
                continue

        # Skip weekends / out-of-session
        if row.name.weekday() in WEEKEND_DAYS or not is_session_allowed(row.name.time()):
            continue

        if any(pd.isna([row['supertrend_signal'], row['adx'], row['rsi'], row['close']])):
            continue

        sig_cur = row['supertrend_signal']
        sig_pre = prev['supertrend_signal']
        price = row['close']

        # ENTRY
        if sig_cur == sig_pre and row['adx'] >= params["adx_threshold"] and \
           params["rsi_oversold"] <= row['rsi'] <= params["rsi_overbought"]:

            if sig_cur == "buy" and position != "buy":
                if position == "sell":
                    pnl = (entry - price) * (LOT_SIZE * contract_size)
                    balance += pnl
                position = "buy"
                entry = price
                stop_loss = entry - params["stop_loss_pts"] * point

            elif sig_cur == "sell" and position != "sell":
                if position == "buy":
                    pnl = (price - entry) * (LOT_SIZE * contract_size)
                    balance += pnl
                position = "sell"
                entry = price
                stop_loss = entry + params["stop_loss_pts"] * point

        # TRAILING + EXIT
        if position == "buy":
            prof_pts = (price - entry) / point
            if prof_pts >= params["trailing_trigger_pts"]:
                stop_loss = max(stop_loss, price - params["trailing_dist_pts"] * point)
            if price <= stop_loss:
                pnl = (price - entry) * (LOT_SIZE * contract_size)
                balance += pnl
                position = None

        elif position == "sell":
            prof_pts = (entry - price) / point
            if prof_pts >= params["trailing_trigger_pts"]:
                stop_loss = min(stop_loss, price + params["trailing_dist_pts"] * point)
            if price >= stop_loss:
                pnl = (entry - price) * (LOT_SIZE * contract_size)
                balance += pnl
                position = None

    return {"profit": balance - START_BALANCE, "params": params}

def backtest_symbol_timeframe(symbol, timeframe, df_raw):
    """
    Build tasks and run simulate_params in parallel.
    Returns best params + profit.
    """
    # Pre-serialize data once
    records = df_raw.to_dict('records')

    # Symbol META for SL ranges
    if not mt5.symbol_select(symbol, True):
        raise RuntimeError(f"Cannot select {symbol}")
    info = mt5.symbol_info(symbol)
    if info is None:
        raise RuntimeError(f"No symbol info for {symbol}")

    point = info.point
    contract_size = info.trade_contract_size
    step_eur = max(1, int((10.0 / (LOT_SIZE * contract_size * point)) + 0.5))
    risk_amt = START_BALANCE * 0.01
    max_sl = int(risk_amt / (LOT_SIZE * contract_size * point))
    min_sl = info.trade_stops_level + 1
    max_sl = max(max_sl, min_sl)

    tasks = []
    for atr_p, mult, adx_p, adx_th, rsi_p, rsi_lo, rsi_hi in product(
            range(5, 15), range(2, 6),
            range(10, 20, 5), range(20, 35, 5),
            range(10, 20, 5), range(25, 40, 5),
            range(60, 75, 5)
    ):
        for sl in range(min_sl, max_sl + 1, step_eur):
            for trig in range(step_eur, max_sl + 1, step_eur):
                for trail in range(step_eur, trig + 1, step_eur):
                    p = {
                        "supertrend_period":     atr_p,
                        "supertrend_multiplier": mult,
                        "adx_period":            adx_p,
                        "adx_threshold":         adx_th,
                        "rsi_period":            rsi_p,
                        "rsi_oversold":          rsi_lo,
                        "rsi_overbought":        rsi_hi,
                        "stop_loss_pts":         sl,
                        "trailing_trigger_pts":  trig,
                        "trailing_dist_pts":     trail
                    }
                    tasks.append((symbol, timeframe, p, records))

    # limit to one fewer than total cores
    num_workers = max(1, int(cpu_count()/2))
    log_info(f"Starting pool with {num_workers} workers (out of {cpu_count()} cores)")
    with Pool(processes=num_workers, initializer=worker_init) as pool:
        results = pool.map(simulate_params, tasks)

    best = max(results, key=lambda x: x["profit"])
    return best["params"], best["profit"]

if __name__ == "__main__":
    if not initialize_mt5():
        log_error("MT5 initialization failed.")
        exit()

    overall = {"best_profit": -float('inf'), "symbol": None, "timeframe": None, "params": None}

    for symbol in SYMBOL_LIST:
        for timeframe in TIMEFRAME_LIST:
            log_info(f"Loading data for {symbol} @ {timeframe}...")
            df = fetch_historical_data(symbol, timeframe, BACKTEST_START_DATE, BACKTEST_END_DATE)
            if df.empty:
                log_error(f"No data for {symbol} @ {timeframe}")
                continue

            df = df[(df['time'] >= pd.to_datetime(BACKTEST_START_DATE)) &
                    (df['time'] <= pd.to_datetime(BACKTEST_END_DATE))]
            if df.empty:
                log_error(f"No data in range for {symbol} @ {timeframe}")
                continue

            log_info(f"Backtesting {symbol} @ {timeframe} on {len(df)} bars...")
            try:
                best_p, best_pf = backtest_symbol_timeframe(symbol, timeframe, df)
            except Exception as e:
                log_error(f"Error backtesting {symbol}@{timeframe}: {e}")
                continue

            if best_pf > overall["best_profit"]:
                overall.update({
                    "best_profit": best_pf,
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "params": best_p
                })

    os.makedirs("results", exist_ok=True)
    with open("results/best_params.json", "w") as f:
        json.dump(overall, f, indent=4)

    log_info(f"[DONE] Best result: {overall}")
    shutdown_mt5()
