# backtester.py — original two-stage search + news (entries-only) + fixed-setup mode
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

# NEWS: fixed module wired to Investing.com Ultimate API
from news_filters import (
    build_news_filters_for_backtest,
    bar_blocked_by_news,
    NEWS_FILTERS_VERSION,
)

# Optional: export news env to workers (if present in config)
try:
    apply_news_env_from_config()
except Exception:
    pass


def worker_init():
    """Initialize MT5 in each worker process."""
    if not mt5.initialize():
        log_error("MT5 initialization failed in worker")


def is_session_allowed(t: datetime.time) -> bool:
    """Check if time t falls within any of the allowed trading sessions."""
    for start, end in ALLOWED_SESSIONS:
        if start <= t <= end:
            return True
    return False


def _parse_timeframe(tf_raw):
    """Accept ints like 1,5,15,30,60,240,1440 or strings 'M1','M5','M15','H1','H4','D1'."""
    if isinstance(tf_raw, int):
        mapping = {
            1: mt5.TIMEFRAME_M1,
            5: mt5.TIMEFRAME_M5,
            15: mt5.TIMEFRAME_M15,
            30: mt5.TIMEFRAME_M30,
            60: mt5.TIMEFRAME_H1,
            240: mt5.TIMEFRAME_H4,
            1440: mt5.TIMEFRAME_D1,
        }
        return mapping.get(tf_raw, mt5.TIMEFRAME_M5)
    if isinstance(tf_raw, str):
        s = tf_raw.strip().upper()
        mapping = {
            "M1": mt5.TIMEFRAME_M1,
            "M5": mt5.TIMEFRAME_M5,
            "M15": mt5.TIMEFRAME_M15,
            "M30": mt5.TIMEFRAME_M30,
            "H1": mt5.TIMEFRAME_H1,
            "H4": mt5.TIMEFRAME_H4,
            "D1": mt5.TIMEFRAME_D1,
        }
        # also allow plain numbers in string
        if s.isdigit():
            return _parse_timeframe(int(s))
        return mapping.get(s, mt5.TIMEFRAME_M5)
    return mt5.TIMEFRAME_M5


def simulate_params(task):
    """
    Run a single backtest for the given parameter set.
    task = (symbol, timeframe, params_dict, raw_records, full_days, pauses)
    """
    symbol, timeframe, params, records, full_days, pauses = task

    # Reconstruct DataFrame from raw records
    df = pd.DataFrame.from_records(records)
    if 'time' not in df.columns:
        return {"profit": -float('inf'), "params": params}
    df['time'] = pd.to_datetime(df['time'])

    df = calculate_indicators(df, params)
    if df is None or df.empty:
        return {"profit": -float('inf'), "params": params}

    # Ensure DateTimeIndex
    if not isinstance(df.index, pd.DatetimeIndex):
        try:
            df.set_index('time', inplace=True)
        except Exception:
            return {"profit": -float('inf'), "params": params}

    # Ensure symbol is tradable
    if not mt5.symbol_select(symbol, True):
        return {"profit": -float('inf'), "params": params}
    info = mt5.symbol_info(symbol)
    if info is None:
        return {"profit": -float('inf'), "params": params}

    point = info.point
    contract_size = info.trade_contract_size

    balance = START_BALANCE
    position = None            # None / "buy" / "sell"
    entry_price = 0.0
    stop_loss = 0.0
    risk_mgr = BacktestRiskManager()

    for i in range(1, len(df)):
        row = df.iloc[i]
        prev = df.iloc[i - 1]

        # Daily/total loss checks
        risk_mgr.update_day(row.name, balance)
        if FUNDED_MODE:
            if risk_mgr.is_max_total_loss_exceeded(balance):
                balance = -float('inf')
                break
            if risk_mgr.is_daily_loss_exceeded(balance):
                # still allow exits on this bar, entries blocked below
                pass

        # --- Compute blocks (do NOT skip bar) ---
        session_block = (row.name.weekday() in WEEKEND_DAYS) or (not is_session_allowed(row.name.time()))
        news_block = False
        if USE_NEWS_FILTER:
            bar_dt = row.name.to_pydatetime() if hasattr(row.name, "to_pydatetime") else row.name
            news_block = bar_blocked_by_news(bar_dt, full_days, pauses)

        # Indicator availability (for entries & trailing trigger)
        ind_nan = any(pd.isna([
            row.get('supertrend_signal', pd.NA),
            row.get('adx', pd.NA),
            row.get('rsi', pd.NA),
            row.get('close', pd.NA),
        ]))

        # Read signals/price
        sig_cur  = None if ind_nan else row['supertrend_signal']
        sig_prev = None if ind_nan else prev.get('supertrend_signal', None)
        price    = row.get('close', None)
        if price is None or pd.isna(price):
            continue

        # ===== TRAILING / EXIT — always evaluate =====
        if position == "buy":
            profit_pts = (price - entry_price) / point
            if not ind_nan and profit_pts >= params["trailing_trigger_pts"]:
                stop_loss = max(stop_loss, price - params["trailing_dist_pts"] * point)
            if price <= stop_loss:
                pnl = (price - entry_price) * (LOT_SIZE * contract_size)
                balance += pnl
                position = None

        elif position == "sell":
            profit_pts = (entry_price - price) / point
            if not ind_nan and profit_pts >= params["trailing_trigger_pts"]:
                stop_loss = min(stop_loss, price + params["trailing_dist_pts"] * point)
            if price >= stop_loss:
                pnl = (entry_price - price) * (LOT_SIZE * contract_size)
                balance += pnl
                position = None

        # ===== ENTRY — only if NOT blocked =====
        if not news_block and not session_block and not ind_nan:
            if sig_cur == sig_prev and row['adx'] >= params["adx_threshold"] \
               and params["rsi_oversold"] <= row['rsi'] <= params["rsi_overbought"]:

                if sig_cur == "buy" and position != "buy":
                    if position == "sell":
                        pnl = (entry_price - price) * (LOT_SIZE * contract_size)
                        balance += pnl
                    position = "buy"
                    entry_price = price
                    stop_loss = entry_price - params["stop_loss_pts"] * point

                elif sig_cur == "sell" and position != "sell":
                    if position == "buy":
                        pnl = (price - entry_price) * (LOT_SIZE * contract_size)
                        balance += pnl
                    position = "sell"
                    entry_price = price
                    stop_loss = entry_price + params["stop_loss_pts"] * point

    return {"profit": balance - START_BALANCE, "params": params}


def backtest_symbol_timeframe(symbol, timeframe, df_raw, full_days, pauses):
    """
    Two-stage grid search (original behavior).
    """
    # Serialize raw data once
    records = df_raw.to_dict('records')

    # Fetch symbol info for SL bounds
    if not mt5.symbol_select(symbol, True):
        raise RuntimeError(f"Cannot select symbol {symbol}")
    info = mt5.symbol_info(symbol)
    if info is None:
        raise RuntimeError(f"No symbol info for {symbol}")

    point = info.point
    contract_size = info.trade_contract_size

    # --- Percent-of-balance pacing (faster & scales with balance) ---
    # 1) Step size in points: each SL step ≈ STAGE2_STEP_RISK_PCT% of balance
    step_risk_amt = START_BALANCE * (STAGE2_STEP_RISK_PCT / 100.0)
    step_pts = max(1, int(round(step_risk_amt / (LOT_SIZE * contract_size * point))))

    # 2) SL upper bound from max risk percent (default 1% of balance)
    max_risk_amt = START_BALANCE * (STAGE2_MAX_RISK_PCT / 100.0)
    max_sl = int(max_risk_amt / (LOT_SIZE * contract_size * point))

    # 3) Respect broker stops level
    min_sl = info.trade_stops_level + 1
    max_sl = max(max_sl, min_sl)

    # --- Stage 1: Indicator-only search with SL/TRAIL = minimum ---
    default_sl = min_sl
    default_trig = min_sl
    default_dist = min_sl

    stage1_tasks = []
    for atr_p, mult, adx_p, adx_th, rsi_p, rsi_lo, rsi_hi in product(
        range(5, 15), range(2, 6),
        range(10, 20, 5), range(20, 35, 5),
        range(10, 20, 5), range(25, 40, 5),
        range(60, 75, 5)
    ):
        params = {
            "supertrend_period":     atr_p,
            "supertrend_multiplier": mult,
            "adx_period":            adx_p,
            "adx_threshold":         adx_th,
            "rsi_period":            rsi_p,
            "rsi_oversold":          rsi_lo,
            "rsi_overbought":        rsi_hi,
            "stop_loss_pts":         default_sl,
            "trailing_trigger_pts":  default_trig,
            "trailing_dist_pts":     default_dist
        }
        stage1_tasks.append((symbol, timeframe, params, records, full_days, pauses))

    log_info(f"[STAGE 1] Indicator scan with SL/TRAIL = {min_sl} pts")
    num_workers = max(1, int(cpu_count()*0.7))
    log_info(f"Using {num_workers} worker processes out of {cpu_count()} logical CPUs")
    with Pool(num_workers, initializer=worker_init) as pool:
        results1 = pool.map(simulate_params, stage1_tasks)

    best1 = max(results1, key=lambda x: x["profit"])
    best_indicators = best1["params"]
    log_info(f"[STAGE 1] Best indicators: {best_indicators}, profit={best1['profit']:.2f}")

    # --- Stage 2: SL/TRAIL fine-grained search using best indicators ---
    stage2_tasks = []
    for sl in range(min_sl, max_sl + 1, step_pts):
        for trig in range(step_pts, max_sl + 1, step_pts):
            for dist in range(step_pts, trig + 1, step_pts):
                p = best_indicators.copy()
                p.update({
                    "stop_loss_pts":        sl,
                    "trailing_trigger_pts": trig,
                    "trailing_dist_pts":    dist
                })
                stage2_tasks.append((symbol, timeframe, p, records, full_days, pauses))

    log_info(f"[STAGE 2] SL/TRAIL grid: SL {min_sl}->{max_sl} step {step_pts}")
    with Pool(num_workers, initializer=worker_init) as pool:
        results2 = pool.map(simulate_params, stage2_tasks)

    best2 = max(results2, key=lambda x: x["profit"])
    final_params = best2["params"]
    log_info(
        f"[STAGE 2] Best SL={final_params['stop_loss_pts']} pts, "
        f"Trigger={final_params['trailing_trigger_pts']} pts, "
        f"Distance={final_params['trailing_dist_pts']} pts, "
        f"profit={best2['profit']:.2f}"
    )

    return final_params, best2["profit"]


if __name__ == "__main__":
    if not initialize_mt5():
        log_error("MT5 initialization failed.")
        exit()

    # NEWS: build once for the whole backtest range
    if USE_NEWS_FILTER:
        try:
            start_dt = pd.to_datetime(BACKTEST_START_DATE).to_pydatetime()
            end_dt   = pd.to_datetime(BACKTEST_END_DATE).to_pydatetime()
            log_info(f"[NEWS] news_filters version: {NEWS_FILTERS_VERSION}")
            full_days, pauses = build_news_filters_for_backtest(start_dt, end_dt)
            full_days = set(full_days or [])
            if not isinstance(pauses, dict):
                pauses = {}
            log_info(f"[NEWS] full_days={len(full_days)} days, pause_days={len(pauses)}")
        except Exception as e:
            log_error(f"[NEWS] Failed to build news filters: {e}")
            full_days, pauses = set(), {}
    else:
        full_days, pauses = set(), {}

    overall = {
        "best_profit": -float('inf'),
        "symbol": None,
        "timeframe": None,
        "params": None
    }

    # ===== NEW: Full fixed setup (symbol + timeframe + params) =====
    if USE_FIXED_SETUP:
        sym = FIXED_SETUP.get("symbol", "GBPUSD")
        tf_raw = FIXED_SETUP.get("timeframe", 5)
        tf = _parse_timeframe(tf_raw)
        params = FIXED_SETUP.get("params", {})
        log_info(f"[MODE] Fixed-setup mode — {sym}@{tf_raw} with params: {params}")

        df = fetch_historical_data(sym, tf, BACKTEST_START_DATE, BACKTEST_END_DATE)
        if df.empty:
            log_error(f"No data for {sym} @ {tf_raw}")
        else:
            df = df[(df['time'] >= pd.to_datetime(BACKTEST_START_DATE)) &
                    (df['time'] <= pd.to_datetime(BACKTEST_END_DATE))]
            if df.empty:
                log_error(f"No bars in range for {sym} @ {tf_raw}")
            else:
                records = df.to_dict('records')
                res = simulate_params((sym, tf, params, records, full_days, pauses))
                profit = res.get("profit", -1e12)
                overall.update({
                    "best_profit": profit,
                    "symbol": sym,
                    "timeframe": tf_raw,
                    "params": params
                })
                log_info(f"[FIXED-SETUP] {sym}@{tf_raw}: profit={profit:.2f}")

    # ===== Else: older fixed-params across lists =====
    elif USE_FIXED_PARAMS:
        log_info("[MODE] Fixed-params mode — testing FIXED_PARAMS on all symbols/timeframes.")
        log_info(f"[MODE] FIXED_PARAMS = {FIXED_PARAMS}")
        for sym in SYMBOL_LIST:
            for tf in TIMEFRAME_LIST:
                log_info(f"Loading data for {sym} @ {tf}...")
                df = fetch_historical_data(sym, tf, BACKTEST_START_DATE, BACKTEST_END_DATE)
                if df.empty:
                    log_error(f"No data for {sym} @ {tf}")
                    continue
                df = df[(df['time'] >= pd.to_datetime(BACKTEST_START_DATE)) &
                        (df['time'] <= pd.to_datetime(BACKTEST_END_DATE))]
                if df.empty:
                    log_error(f"No bars in range for {sym} @ {tf}")
                    continue

                records = df.to_dict('records')
                res = simulate_params((sym, tf, FIXED_PARAMS, records, full_days, pauses))
                profit = res.get("profit", -1e12)
                log_info(f"[FIXED] {sym}@{tf}: profit={profit:.2f}")
                if profit > overall["best_profit"]:
                    overall.update({
                        "best_profit": profit,
                        "symbol": sym,
                        "timeframe": tf,
                        "params": FIXED_PARAMS
                    })

    # ===== Else: original search mode =====
    else:
        for sym in SYMBOL_LIST:
            for tf in TIMEFRAME_LIST:
                log_info(f"Loading data for {sym} @ {tf}...")
                df = fetch_historical_data(sym, tf, BACKTEST_START_DATE, BACKTEST_END_DATE)
                if df.empty:
                    log_error(f"No data for {sym} @ {tf}")
                    continue

                df = df[(df['time'] >= pd.to_datetime(BACKTEST_START_DATE)) &
                        (df['time'] <= pd.to_datetime(BACKTEST_END_DATE))]
                if df.empty:
                    log_error(f"No bars in range for {sym} @ {tf}")
                    continue

                log_info(f"Backtesting {sym} @ {tf} on {len(df)} bars...")
                try:
                    params, profit = backtest_symbol_timeframe(sym, tf, df, full_days, pauses)
                except Exception as e:
                    log_error(f"Error backtesting {sym} @ {tf}: {e}")
                    continue

                if profit > overall["best_profit"]:
                    overall.update({
                        "best_profit": profit,
                        "symbol": sym,
                        "timeframe": tf,
                        "params": params
                    })

    os.makedirs("results", exist_ok=True)
    with open("results/best_params.json", "w") as f:
        json.dump(overall, f, indent=4)

    log_info(f"[DONE] Best result: {overall}")
    shutdown_mt5()
