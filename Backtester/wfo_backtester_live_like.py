
"""
wfo_backtester_live_like.py

Walk-Forward Optimization (WFO) for your Tradingbot_V4 project.

Goal:
- TRAIN window: run the SAME grid search logic as backtester.py (Stage-1 indicator scan + Stage-2 ATR multipliers tune)
- TEST window: run a "live-like" simulation, mirroring the backtester/live execution model:
    * signals evaluated on bar close
    * fills on next tick (bid/ask) if tick data is available; otherwise uses synthetic bid/ask derived from bar close + spread
    * trailing stop checked on intrabar ticks (real ticks if available, otherwise synthetic M1 path points)
    * same session/news gating rules
    * same ATR-based SL/TRAIL distances locked at entry bar's ATR and scaled by BASE_BALANCE/RISK_EXPONENT
    * same optional risk-based lot sizing (risk_cash / (sl_dist * contract_size))

This file is intentionally separate from backtester.py so your classic workflow remains unchanged.
It reuses the EXACT Stage-1 indicator grid defined in backtester.py and the Stage-2 lists from config.py.

Outputs:
- results/wfo_live_like/wfo_summary.csv
- results/wfo_live_like/steps/step_XXXX_train_best.json
- results/wfo_live_like/steps/step_XXXX_test_result.json
- results/wfo_live_like/steps/step_XXXX_test_trades.csv
- results/wfo_live_like/stitched_equity.csv

Usage (defaults read from config.py for symbols/timeframes and backtest dates):
    python wfo_backtester_live_like.py

Optional environment variables:
    WFO_START="YYYY-MM-DD"
    WFO_END="YYYY-MM-DD"
    WFO_TRAIN_DAYS="30"
    WFO_TEST_DAYS="7"
    WFO_STRIDE_DAYS="7"
    WFO_MAX_STEPS="0"           (0 = no limit)
    WFO_OUT_DIR="results/wfo_live_like"
    WFO_SCORE_MODE="profit"     or "profit_minus_dd"
    WFO_DD_WEIGHT="1.5"
    WFO_USE_NEWS_FILTER="1"     or "0"
    MAX_WORKERS_HINT="11"       (already used in config.py)
"""

from __future__ import annotations

import os
import json
import csv
from dataclasses import dataclass
from datetime import datetime, timedelta
from itertools import product
from multiprocessing import Pool, cpu_count
from typing import Any, Dict, List, Tuple, Optional
import numpy as np
import pandas as pd
import MetaTrader5 as mt5
from config import *  # uses your existing config constants
from utils import log_info, log_error
from strategy import calculate_indicators
from news_filters import build_news_filters_for_backtest, bar_blocked_by_news
from mt5_connector import fetch_historical_data


# --------------------------- small safe getters ---------------------------

def COMMISSION_PER_LOT_SAFE() -> float:
    try:
        return float(COMMISSION_PER_LOT)
    except Exception:
        return 0.0

def ENABLE_RISK_SIZING_SAFE() -> bool:
    try:
        return bool(ENABLE_RISK_SIZING)
    except Exception:
        return False

def RISK_PER_TRADE_SAFE() -> float:
    try:
        return float(RISK_PER_TRADE)
    except Exception:
        return 0.0

def BASE_BALANCE_SAFE() -> float:
    try:
        return float(BASE_BALANCE)
    except Exception:
        return 0.0

def RISK_EXPONENT_SAFE() -> float:
    try:
        return float(RISK_EXPONENT)
    except Exception:
        return 0.0

def START_BALANCE_SAFE() -> float:
    try:
        return float(START_BALANCE)
    except Exception:
        return 100000.0


def _round_trip_commission(lots: float) -> float:
    return 2.0 * COMMISSION_PER_LOT_SAFE() * float(lots)


# --------------------------- sessions ---------------------------

def is_session_allowed(ts: datetime) -> bool:
    """Same session gating as your classic backtester: ALLOWED_SESSIONS in local time."""
    try:
        sessions = ALLOWED_SESSIONS
    except Exception:
        sessions = []
    if not sessions:
        return True
    t = ts.time()
    for start_t, end_t in sessions:
        if start_t <= t <= end_t:
            return True
    return False


# --------------------------- tick helpers (cached per run) ---------------------------

def _ensure_dirs(out_dir: str):
    os.makedirs(out_dir, exist_ok=True)
    os.makedirs(os.path.join(out_dir, "steps"), exist_ok=True)
    os.makedirs("results/tick_cache", exist_ok=True)


def _tick_cache_path(symbol: str, start_dt: datetime, end_dt: datetime) -> str:
    s = start_dt.strftime("%Y%m%d%H%M%S")
    e = end_dt.strftime("%Y%m%d%H%M%S")
    safe_sym = symbol.replace("/", "_")
    return os.path.join("results/tick_cache", f"{safe_sym}_{s}_{e}.parquet")


def fetch_ticks(symbol: str, start_dt: datetime, end_dt: datetime) -> pd.DataFrame:
    """
    Fetch ticks from MT5 and cache to disk for speed.
    Returns dataframe indexed by time (pd.Timestamp) with columns bid/ask.
    """
    path = _tick_cache_path(symbol, start_dt, end_dt)
    if os.path.exists(path):
        try:
            ticks = pd.read_parquet(path)
            if isinstance(ticks.index, pd.DatetimeIndex):
                return ticks
        except Exception as e:
            log_error(f"[TICKS] Failed loading cache {path}: {e}")

    # MT5 tick download
    utc_from = start_dt
    utc_to = end_dt
    try:
        raw = mt5.copy_ticks_range(symbol, utc_from, utc_to, mt5.COPY_TICKS_ALL)
    except Exception as e:
        log_error(f"[TICKS] copy_ticks_range failed: {e}")
        return pd.DataFrame(columns=["bid", "ask"])

    if raw is None or len(raw) == 0:
        ticks = pd.DataFrame(columns=["bid", "ask"])
    else:
        ticks = pd.DataFrame(raw)
        # time in seconds; time_msc exists too
        if "time_msc" in ticks.columns:
            ticks["time"] = pd.to_datetime(ticks["time_msc"], unit="ms", utc=False)
        else:
            ticks["time"] = pd.to_datetime(ticks["time"], unit="s", utc=False)
        ticks = ticks.set_index("time").sort_index()
        # normalize columns
        if "bid" not in ticks.columns and "last" in ticks.columns:
            ticks["bid"] = ticks["last"]
        if "ask" not in ticks.columns:
            # naive ask = bid if missing
            ticks["ask"] = ticks.get("bid", ticks.get("last", 0.0))
        ticks = ticks[["bid", "ask"]].astype(float)

    try:
        ticks.to_parquet(path)
    except Exception as e:
        log_error(f"[TICKS] Failed saving cache {path}: {e}")
    return ticks


def m1_path_points(row: pd.Series) -> List[Tuple[datetime, float, float]]:
    """
    Synthetic intrabar path for M1 when no ticks are available.
    Returns sequence of (t, bid, ask) points.
    Uses open/high/low/close and spread.
    """
    o = float(row["open"]); h = float(row["high"]); l = float(row["low"]); c = float(row["close"])
    spread_pts = float(row.get("spread", 0.0))
    # approximate point size from symbol digits? We may not have it; keep spread in price units if already.
    # In MT5 rates, spread is in points. We'll convert later with symbol point; here keep as "points" and adjust in caller.
    # We'll return bid/ask in PRICE UNITS; caller will pass correct point conversion.
    # We'll fill "ask = bid + spread_pts*point" in caller.
    return [
        (None, o, o),
        (None, h, h),
        (None, l, l),
        (None, c, c),
    ]


# --------------------------- ATR series (Wilder) ---------------------------

def compute_atr_series(df: pd.DataFrame, period: int) -> pd.Series:
    """Wilder ATR in *price units*."""
    h = df["high"].astype(float)
    l = df["low"].astype(float)
    c = df["close"].astype(float)
    prev_c = c.shift(1)

    tr1 = h - l
    tr2 = (h - prev_c).abs()
    tr3 = (l - prev_c).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    atr = tr.ewm(alpha=1.0/period, adjust=False, min_periods=period).mean()
    return atr


def pip_size(symbol: str) -> float:
    info = mt5.symbol_info(symbol)
    if not info:
        return 0.0001
    return 0.01 if info.digits in (2, 3) else 0.0001


def quantize_lot(symbol_info, lots: float) -> float:
    """Same quantization as in backtester.py."""
    if symbol_info is None:
        return max(0.01, round(lots, 2))
    step = getattr(symbol_info, "volume_step", 0.01) or 0.01
    minv = getattr(symbol_info, "volume_min", 0.01) or 0.01
    maxv = getattr(symbol_info, "volume_max", 1000.0) or 1000.0
    lots = max(minv, min(maxv, lots))
    # quantize to step
    q = round(lots / step) * step
    # avoid float artifacts
    q = float(f"{q:.8f}")
    return q


# --------------------------- live-like simulator (windowed) ---------------------------

def worker_init():
    if not mt5.initialize():
        log_error("MT5 initialization failed in worker")


def simulate_params_window(task):
    """
    Windowed version of backtester.simulate_params with live-like behavior.

    task = (symbol, timeframe, params, full_days, pauses, start_dt, end_dt)
    Returns dict with:
        profit, symbol, timeframe, params, trades, _trades, max_drawdown, equity_end
    """
    symbol, timeframe, params, full_days, pauses, start_dt, end_dt = task

    if not mt5.initialize():
        log_error("MT5 init failed in worker")
        return {"profit": 0.0, "symbol": symbol, "timeframe": timeframe, "params": params, "trades": 0, "_trades": []}

    df = fetch_historical_data(symbol, timeframe, start_dt, end_dt)
    if df is None or df.empty:
        mt5.shutdown()
        return {"profit": 0.0, "symbol": symbol, "timeframe": timeframe, "params": params, "trades": 0, "_trades": []}

    df["symbol"] = symbol
    df = calculate_indicators(df, params)

    # ensure datetime index
    if "time" in df.columns:
        df["time"] = pd.to_datetime(df["time"])
        df = df.set_index("time", drop=True)
    if not isinstance(df.index, pd.DatetimeIndex):
        mt5.shutdown()
        return {"profit": 0.0, "symbol": symbol, "timeframe": timeframe, "params": params, "trades": 0, "_trades": []}

    # ATR points for entry bar locking
    atr_period = int(params.get("atr_period", params.get("supertrend_period", 14)))
    atr_series = compute_atr_series(df.reset_index(), atr_period)
    # align to df index
    df["atr_price"] = atr_series.values
    info = mt5.symbol_info(symbol)
    if info is None:
        mt5.shutdown()
        return {"profit": 0.0, "symbol": symbol, "timeframe": timeframe, "params": params, "trades": 0, "_trades": []}

    point = float(info.point)
    contract_size = float(info.trade_contract_size)
    pip = pip_size(symbol) or (10 * point if point > 0 else 0.0001)

    # ticks cache for window (slightly expanded to cover entry/exit around edges)
    tick_start = start_dt - timedelta(minutes=5)
    tick_end = end_dt + timedelta(minutes=5)
    ticks = fetch_ticks(symbol, tick_start, tick_end)

    def next_tick_after(ts: pd.Timestamp):
        if ticks.empty:
            return None
        idx = ticks.index.searchsorted(ts + pd.Timedelta(microseconds=1), side="left")
        if idx >= len(ticks):
            return None
        tt = ticks.iloc[idx]
        return (ticks.index[idx].to_pydatetime(), float(tt["bid"]), float(tt["ask"]))

    def ticks_between(t0: pd.Timestamp, t1: pd.Timestamp):
        if ticks.empty:
            return []
        sl = ticks.loc[(ticks.index > t0) & (ticks.index <= t1)]
        if sl.empty:
            return []
        # list of tuples
        out = []
        for t_idx, r in sl.iterrows():
            out.append((t_idx.to_pydatetime(), float(r["bid"]), float(r["ask"])))
        return out

    # position state
    side = 0                # 0 flat, 1 long, -1 short
    entry_price = 0.0
    entry_time: Optional[datetime] = None
    sl_price: Optional[float] = None
    lots_open = 0.0

    # locked distances for trailing (price units)
    trig_d = 0.0
    dist_d = 0.0

    # balance/equity
    balance = float(START_BALANCE_SAFE())
    equity_curve = [balance]
    peak = balance
    max_dd = 0.0

    trades_log: List[Dict[str, Any]] = []

    # choose ADX/RSI columns by exact period (to match params)
    adx_col = f"ADX_{int(params.get('adx_period', 14))}"
    rsi_col = f"RSI_{int(params.get('rsi_period', 14))}"

    # if not present, fall back to any available
    if adx_col not in df.columns:
        adx_cols = [c for c in df.columns if c.upper().startswith("ADX_")]
        if adx_cols:
            adx_col = adx_cols[0]
    if rsi_col not in df.columns:
        rsi_cols = [c for c in df.columns if c.upper().startswith("RSI_")]
        if rsi_cols:
            rsi_col = rsi_cols[0]

    # main loop: bar by bar
    for i in range(1, len(df)):
        row  = df.iloc[i]
        prev = df.iloc[i - 1]
        ts: pd.Timestamp = df.index[i]
        ts_prev: pd.Timestamp = df.index[i - 1]

        # gating
        if ts.to_pydatetime().weekday() in WEEKEND_DAYS or not is_session_allowed(ts.to_pydatetime()):
            continue
        if bool(os.getenv("WFO_USE_NEWS_FILTER", "1") == "1") and USE_NEWS_FILTER:
            try:
                if bar_blocked_by_news(ts.to_pydatetime(), full_days, pauses):
                    continue
            except Exception:
                pass

        # manage open position on intrabar ticks
        if side != 0:
            intraticks = ticks_between(ts_prev, ts)
            iter_seq = intraticks
            if not iter_seq:
                # build synthetic path; convert to bid/ask with spread in points
                spread_pts = float(row.get("spread", 0.0))
                synth = []
                for _, bid0, _ in m1_path_points(row):
                    bid = float(bid0)
                    ask = bid + spread_pts * point
                    t_fill = ts_prev.to_pydatetime() + timedelta(seconds=15)  # rough
                    synth.append((t_fill, bid, ask))
                iter_seq = synth

            for t_idx, bid, ask in iter_seq:
                # trailing update
                if side == 1:
                    if trig_d > 0.0 and dist_d > 0.0:
                        move = (bid - entry_price)
                        if move >= trig_d:
                            sl_price = max(sl_price if sl_price is not None else -1e9, bid - dist_d)
                    if sl_price is not None and bid <= sl_price:
                        pnl = (bid - entry_price) * contract_size * lots_open - _round_trip_commission(lots_open)
                        trades_log.append({
                            "open_time": entry_time, "close_time": t_idx,
                            "symbol": symbol, "timeframe": timeframe, "side": "buy",
                            "lots": lots_open, "entry_price": entry_price, "exit_price": bid,
                            "commission_roundtrip": _round_trip_commission(lots_open),
                            "profit": pnl, "pnl_pips": (bid - entry_price) / pip,
                            "exit_reason": "stop_loss",
                        })
                        balance += pnl
                        equity_curve.append(balance)
                        peak = max(peak, balance)
                        max_dd = max(max_dd, peak - balance)
                        side = 0; entry_price = 0.0; entry_time = None; sl_price = None; lots_open = 0.0
                        trig_d = 0.0; dist_d = 0.0
                        break
                else:
                    if trig_d > 0.0 and dist_d > 0.0:
                        move = (entry_price - ask)
                        if move >= trig_d:
                            sl_price = min(sl_price if sl_price is not None else 1e9, ask + dist_d)
                    if sl_price is not None and ask >= sl_price:
                        pnl = (entry_price - ask) * contract_size * lots_open - _round_trip_commission(lots_open)
                        trades_log.append({
                            "open_time": entry_time, "close_time": t_idx,
                            "symbol": symbol, "timeframe": timeframe, "side": "sell",
                            "lots": lots_open, "entry_price": entry_price, "exit_price": ask,
                            "commission_roundtrip": _round_trip_commission(lots_open),
                            "profit": pnl, "pnl_pips": (entry_price - ask) / pip,
                            "exit_reason": "stop_loss",
                        })
                        balance += pnl
                        equity_curve.append(balance)
                        peak = max(peak, balance)
                        max_dd = max(max_dd, peak - balance)
                        side = 0; entry_price = 0.0; entry_time = None; sl_price = None; lots_open = 0.0
                        trig_d = 0.0; dist_d = 0.0
                        break

        # bar-close signal evaluation (exact style)
        sig_prev = str(prev.get("supertrend_signal", ""))
        sig_cur  = str(row.get("supertrend_signal", ""))

        adx = float(row.get(adx_col, np.nan))
        rsi = float(row.get(rsi_col, np.nan))

        adx_ok = adx >= float(params.get("adx_threshold", 0))
        rsi_ok = (rsi <= float(params.get("rsi_oversold", -1e9))) or (rsi >= float(params.get("rsi_overbought", 1e9))) or (float(params.get("rsi_oversold", -1e9)) < rsi < float(params.get("rsi_overbought", 1e9)))
        # In your live logic, RSI condition is usually "between oversold/overbought"? Your current live uses rsi_ok = rsi between? 
        # To match classic backtester more closely, use: oversold < rsi < overbought
        rsi_ok = (float(params.get("rsi_oversold", 30.0)) <= rsi <= float(params.get("rsi_overbought", 70.0)))

        enter_long  = (sig_prev == "buy"  and sig_cur == "buy"  and adx_ok and rsi_ok)
        enter_short = (sig_prev == "sell" and sig_cur == "sell" and adx_ok and rsi_ok)

        # fill next tick after bar close (or synth from bar close)
        nxt = next_tick_after(ts)
        if nxt is None:
            spread_pts = float(row.get("spread", 0.0))
            ask = float(row["close"]) + spread_pts * point
            bid = float(row["close"])
            t_fill = (ts.to_pydatetime() + timedelta(microseconds=1))
        else:
            t_fill, bid, ask = nxt

        # flips
        if side == 1 and enter_short:
            pnl = (bid - entry_price) * contract_size * lots_open - _round_trip_commission(lots_open)
            trades_log.append({
                "open_time": entry_time, "close_time": t_fill,
                "symbol": symbol, "timeframe": timeframe, "side": "buy",
                "lots": lots_open, "entry_price": entry_price, "exit_price": bid,
                "commission_roundtrip": _round_trip_commission(lots_open),
                "profit": pnl, "pnl_pips": (bid - entry_price) / pip,
                "exit_reason": "signal_flip",
            })
            balance += pnl
            equity_curve.append(balance)
            peak = max(peak, balance)
            max_dd = max(max_dd, peak - balance)

            # open short
            atr_price = float(row.get("atr_price", np.nan))
            atr_pts = (atr_price / point) if (point > 0 and np.isfinite(atr_price)) else 0.0
            risk_scale = (balance / BASE_BALANCE_SAFE()) ** RISK_EXPONENT_SAFE() if BASE_BALANCE_SAFE() > 0 else 1.0
            k_sl = float(params["k_sl"]); k_trg = float(params["k_trg"]); k_dist = float(params["k_dist"])
            sl_dist = atr_pts * point * k_sl * risk_scale
            trig_d = atr_pts * point * k_trg * risk_scale
            dist_d = atr_pts * point * k_dist * risk_scale

            sl_price = ask + sl_dist if sl_dist > 0 else None

            lots = float(LOT_SIZE)
            if ENABLE_RISK_SIZING_SAFE():
                risk_cash = balance * RISK_PER_TRADE_SAFE()
                denom = max(1e-9, sl_dist * contract_size)
                lots = quantize_lot(info, risk_cash / denom)
            lots_open = lots
            side = -1
            entry_price = ask
            entry_time = t_fill

        elif side == -1 and enter_long:
            pnl = (entry_price - ask) * contract_size * lots_open - _round_trip_commission(lots_open)
            trades_log.append({
                "open_time": entry_time, "close_time": t_fill,
                "symbol": symbol, "timeframe": timeframe, "side": "sell",
                "lots": lots_open, "entry_price": entry_price, "exit_price": ask,
                "commission_roundtrip": _round_trip_commission(lots_open),
                "profit": pnl, "pnl_pips": (entry_price - ask) / pip,
                "exit_reason": "signal_flip",
            })
            balance += pnl
            equity_curve.append(balance)
            peak = max(peak, balance)
            max_dd = max(max_dd, peak - balance)

            atr_price = float(row.get("atr_price", np.nan))
            atr_pts = (atr_price / point) if (point > 0 and np.isfinite(atr_price)) else 0.0
            risk_scale = (balance / BASE_BALANCE_SAFE()) ** RISK_EXPONENT_SAFE() if BASE_BALANCE_SAFE() > 0 else 1.0
            k_sl = float(params["k_sl"]); k_trg = float(params["k_trg"]); k_dist = float(params["k_dist"])
            sl_dist = atr_pts * point * k_sl * risk_scale
            trig_d = atr_pts * point * k_trg * risk_scale
            dist_d = atr_pts * point * k_dist * risk_scale

            sl_price = bid - sl_dist if sl_dist > 0 else None

            lots = float(LOT_SIZE)
            if ENABLE_RISK_SIZING_SAFE():
                risk_cash = balance * RISK_PER_TRADE_SAFE()
                denom = max(1e-9, sl_dist * contract_size)
                lots = quantize_lot(info, risk_cash / denom)
            lots_open = lots
            side = 1
            entry_price = bid
            entry_time = t_fill

        elif side == 0:
            if enter_long:
                atr_price = float(row.get("atr_price", np.nan))
                atr_pts = (atr_price / point) if (point > 0 and np.isfinite(atr_price)) else 0.0
                risk_scale = (balance / BASE_BALANCE_SAFE()) ** RISK_EXPONENT_SAFE() if BASE_BALANCE_SAFE() > 0 else 1.0
                k_sl = float(params["k_sl"]); k_trg = float(params["k_trg"]); k_dist = float(params["k_dist"])
                sl_dist = atr_pts * point * k_sl * risk_scale
                trig_d = atr_pts * point * k_trg * risk_scale
                dist_d = atr_pts * point * k_dist * risk_scale

                sl_price = bid - sl_dist if sl_dist > 0 else None

                lots = float(LOT_SIZE)
                if ENABLE_RISK_SIZING_SAFE():
                    risk_cash = balance * RISK_PER_TRADE_SAFE()
                    denom = max(1e-9, sl_dist * contract_size)
                    lots = quantize_lot(info, risk_cash / denom)
                lots_open = lots
                side = 1
                entry_price = bid
                entry_time = t_fill

            elif enter_short:
                atr_price = float(row.get("atr_price", np.nan))
                atr_pts = (atr_price / point) if (point > 0 and np.isfinite(atr_price)) else 0.0
                risk_scale = (balance / BASE_BALANCE_SAFE()) ** RISK_EXPONENT_SAFE() if BASE_BALANCE_SAFE() > 0 else 1.0
                k_sl = float(params["k_sl"]); k_trg = float(params["k_trg"]); k_dist = float(params["k_dist"])
                sl_dist = atr_pts * point * k_sl * risk_scale
                trig_d = atr_pts * point * k_trg * risk_scale
                dist_d = atr_pts * point * k_dist * risk_scale

                sl_price = ask + sl_dist if sl_dist > 0 else None

                lots = float(LOT_SIZE)
                if ENABLE_RISK_SIZING_SAFE():
                    risk_cash = balance * RISK_PER_TRADE_SAFE()
                    denom = max(1e-9, sl_dist * contract_size)
                    lots = quantize_lot(info, risk_cash / denom)
                lots_open = lots
                side = -1
                entry_price = ask
                entry_time = t_fill

    # force close at end on last bar close tick approximation
    if side != 0 and len(df) > 0:
        last = df.iloc[-1]
        ts_last = df.index[-1]
        spread_pts = float(last.get("spread", 0.0))
        bid_last = float(last["close"])
        ask_last = bid_last + spread_pts * point

        if side == 1:
            pnl = (bid_last - entry_price) * contract_size * lots_open - _round_trip_commission(lots_open)
            trades_log.append({
                "open_time": entry_time, "close_time": ts_last.to_pydatetime(),
                "symbol": symbol, "timeframe": timeframe, "side": "buy",
                "lots": lots_open, "entry_price": entry_price, "exit_price": bid_last,
                "commission_roundtrip": _round_trip_commission(lots_open),
                "profit": pnl, "pnl_pips": (bid_last - entry_price) / pip,
                "exit_reason": "window_end",
            })
            balance += pnl
        else:
            pnl = (entry_price - ask_last) * contract_size * lots_open - _round_trip_commission(lots_open)
            trades_log.append({
                "open_time": entry_time, "close_time": ts_last.to_pydatetime(),
                "symbol": symbol, "timeframe": timeframe, "side": "sell",
                "lots": lots_open, "entry_price": entry_price, "exit_price": ask_last,
                "commission_roundtrip": _round_trip_commission(lots_open),
                "profit": pnl, "pnl_pips": (entry_price - ask_last) / pip,
                "exit_reason": "window_end",
            })
            balance += pnl

        equity_curve.append(balance)
        peak = max(peak, balance)
        max_dd = max(max_dd, peak - balance)

    mt5.shutdown()
    return {
        "profit": balance - START_BALANCE_SAFE(),
        "equity_end": balance,
        "max_drawdown": float(max_dd),
        "symbol": symbol,
        "timeframe": timeframe,
        "params": params,
        "trades": len(trades_log),
        "_trades": trades_log,
    }


# --------------------------- Stage-1 / Stage-2 using exact classic grids ---------------------------

# Stage-1 grid is hard-coded in backtester.py; we mirror it exactly:
#   atr_p in range(5, 15)
#   mult in range(2, 6)
#   adx_p in range(10, 20, 5)
#   adx_th in range(20, 35, 5)
#   rsi_p in range(10, 20, 5)
#   rsi_lo in range(25, 40, 5)
#   rsi_hi in range(60, 75, 5)
STAGE1_ATR_P = range(5, 15)
STAGE1_MULT = range(2, 6)
STAGE1_ADX_P = range(10, 20, 5)
STAGE1_ADX_TH = range(20, 35, 5)
STAGE1_RSI_P = range(10, 20, 5)
STAGE1_RSI_LO = range(25, 40, 5)
STAGE1_RSI_HI = range(60, 75, 5)


def _workers() -> int:
    hint = int(os.getenv("MAX_WORKERS_HINT", str(getattr(__import__("config"), "MAX_WORKERS_HINT", 11))))
    return max(1, min(hint, cpu_count()))


def _score(res: Dict[str, Any], score_mode: str, dd_weight: float) -> float:
    prof = float(res.get("profit", 0.0))
    if score_mode == "profit_minus_dd":
        dd = float(res.get("max_drawdown", 0.0))
        return prof - dd_weight * dd
    return prof


def stage1_scan_window(symbol: str, timeframe: int, full_days, pauses, start_dt: datetime, end_dt: datetime,
                       score_mode: str, dd_weight: float):
    """Stage-1: scan indicator params; fixed ATR multipliers (S1_K_*)."""
    tasks = []
    for atr_p, mult, adx_p, adx_th, rsi_p, rsi_lo, rsi_hi in product(
        STAGE1_ATR_P, STAGE1_MULT,
        STAGE1_ADX_P, STAGE1_ADX_TH,
        STAGE1_RSI_P, STAGE1_RSI_LO,
        STAGE1_RSI_HI
    ):
        params = {
            "supertrend_period": atr_p,
            "supertrend_multiplier": mult,
            "adx_period": adx_p,
            "adx_threshold": adx_th,
            "rsi_period": rsi_p,
            "rsi_oversold": rsi_lo,
            "rsi_overbought": rsi_hi,
            "k_sl": S1_K_SL,
            "k_trg": S1_K_TRG,
            "k_dist": S1_K_DIST,
            "atr_period": atr_p,
        }
        tasks.append((symbol, timeframe, params, full_days, pauses, start_dt, end_dt))

    workers = _workers()
    with Pool(workers, initializer=worker_init) as pool:
        results = pool.map(simulate_params_window, tasks)

    best = max(results, key=lambda r: _score(r, score_mode, dd_weight))
    return best["params"], best


def stage2_tune_window(symbol: str, timeframe: int, best_indicators: Dict[str, Any], full_days, pauses,
                       start_dt: datetime, end_dt: datetime, score_mode: str, dd_weight: float):
    """Stage-2: tune ATR multipliers (k_sl/k_trg/k_dist) around best indicator set from Stage-1."""
    tasks = []
    for ksl in S2_K_SL_LIST:
        for ktrg in S2_K_TRG_LIST:
            for kdist in S2_K_DIST_LIST:
                p = dict(best_indicators)
                p.update({
                    "k_sl": float(ksl),
                    "k_trg": float(ktrg),
                    "k_dist": float(kdist),
                    "atr_period": int(best_indicators.get("atr_period", best_indicators.get("supertrend_period", 14))),
                })
                tasks.append((symbol, timeframe, p, full_days, pauses, start_dt, end_dt))

    workers = _workers()
    with Pool(workers, initializer=worker_init) as pool:
        results = pool.map(simulate_params_window, tasks)

    best = max(results, key=lambda r: _score(r, score_mode, dd_weight))
    return best["params"], best


# --------------------------- WFO runner ---------------------------

@dataclass
class WFOConfig:
    global_start: datetime
    global_end: datetime
    train_days: int = 30
    test_days: int = 7
    stride_days: int = 7
    out_dir: str = "results/wfo_live_like"
    score_mode: str = "profit_minus_dd"
    dd_weight: float = 1.5
    max_steps: int = 0  # 0 = unlimited


def _parse_date_env(name: str, fallback: str) -> datetime:
    s = os.getenv(name, fallback)
    return pd.to_datetime(s).to_pydatetime()


def iter_steps(cfg: WFOConfig):
    t0 = cfg.global_start
    step = 0
    while True:
        train_start = t0
        train_end = t0 + timedelta(days=cfg.train_days)
        test_start = train_end
        test_end = test_start + timedelta(days=cfg.test_days)

        if test_end > cfg.global_end:
            break

        step += 1
        yield step, train_start, train_end, test_start, test_end

        if cfg.max_steps and step >= cfg.max_steps:
            break

        t0 = t0 + timedelta(days=cfg.stride_days)


def save_trades_csv(path: str, trades: List[Dict[str, Any]]):
    if not trades:
        pd.DataFrame([]).to_csv(path, index=False)
        return
    df = pd.DataFrame(trades)
    # ensure datetimes stringify
    for c in ["open_time", "close_time"]:
        if c in df.columns:
            df[c] = df[c].astype(str)
    df.to_csv(path, index=False)


def run_wfo(cfg: WFOConfig):
    _ensure_dirs(cfg.out_dir)

    summary_csv = os.path.join(cfg.out_dir, "wfo_summary.csv")
    stitched_csv = os.path.join(cfg.out_dir, "stitched_equity.csv")

    with open(summary_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "step",
            "train_start", "train_end", "test_start", "test_end",
            "symbol", "timeframe",
            "train_profit", "train_dd", "train_trades",
            "test_profit", "test_dd", "test_trades",
            "params_json"
        ])

        stitched_equity = []
        equity = float(START_BALANCE_SAFE())
        stitched_equity.append({"step": 0, "time": cfg.global_start.isoformat(), "equity": equity})

        for step, train_start, train_end, test_start, test_end in iter_steps(cfg):
            step_id = f"{step:04d}"
            log_info(f"[WFO] Step {step_id} TRAIN {train_start.date()}→{train_end.date()} | TEST {test_start.date()}→{test_end.date()}")

            # Build news windows once for train+test range (same as your live usage)
            full_days, pauses = set(), {}
            use_news = os.getenv("WFO_USE_NEWS_FILTER", "1") == "1"
            if use_news and USE_NEWS_FILTER:
                try:
                    full_days, pauses = build_news_filters_for_backtest(train_start, test_end)
                except Exception as e:
                    log_error(f"[NEWS] WFO build failed ({e}). Continuing without news filter for this step.")
                    full_days, pauses = set(), {}

            # TRAIN: per symbol/timeframe -> select best overall
            candidates = []
            for symbol in SYMBOL_LIST:
                for timeframe in TIMEFRAME_LIST:
                    best_ind, best1 = stage1_scan_window(
                        symbol, timeframe, full_days, pauses, train_start, train_end,
                        cfg.score_mode, cfg.dd_weight
                    )
                    best_params, best2 = stage2_tune_window(
                        symbol, timeframe, best_ind, full_days, pauses, train_start, train_end,
                        cfg.score_mode, cfg.dd_weight
                    )
                    candidates.append({
                        "symbol": symbol,
                        "timeframe": timeframe,
                        "params": best_params,
                        "train_res": best2,
                    })

            overall = max(candidates, key=lambda c: _score(c["train_res"], cfg.score_mode, cfg.dd_weight))

            train_res = overall["train_res"]
            train_profit = float(train_res.get("profit", 0.0))
            train_dd = float(train_res.get("max_drawdown", 0.0))
            train_trades = int(train_res.get("trades", 0))

            # Save train best
            train_best_path = os.path.join(cfg.out_dir, "steps", f"step_{step_id}_train_best.json")
            with open(train_best_path, "w", encoding="utf-8") as jf:
                json.dump({
                    "step": step,
                    "train_start": train_start.isoformat(),
                    "train_end": train_end.isoformat(),
                    "test_start": test_start.isoformat(),
                    "test_end": test_end.isoformat(),
                    "best": {
                        "symbol": overall["symbol"],
                        "timeframe": overall["timeframe"],
                        "params": overall["params"],
                        "train_profit": train_profit,
                        "train_dd": train_dd,
                        "train_trades": train_trades,
                        "score_mode": cfg.score_mode,
                        "dd_weight": cfg.dd_weight,
                    }
                }, jf, indent=2)

            # TEST: live-like simulation on next window with fixed params (also multiprocessing style)
            test_tasks = [(overall["symbol"], overall["timeframe"], overall["params"], full_days, pauses, test_start, test_end)]
            workers = _workers()
            with Pool(workers, initializer=worker_init) as pool:
                test_results = pool.map(simulate_params_window, test_tasks)
            test_res = test_results[0]

            test_profit = float(test_res.get("profit", 0.0))
            test_dd = float(test_res.get("max_drawdown", 0.0))
            test_trades = int(test_res.get("trades", 0))
            trades_list = test_res.get("_trades", [])

            # Update stitched equity (as if you traded sequentially across steps)
            equity += test_profit
            stitched_equity.append({"step": step, "time": test_end.isoformat(), "equity": equity})

            # Save test result + trades
            test_path = os.path.join(cfg.out_dir, "steps", f"step_{step_id}_test_result.json")
            with open(test_path, "w", encoding="utf-8") as jf:
                json.dump({
                    "step": step,
                    "test_start": test_start.isoformat(),
                    "test_end": test_end.isoformat(),
                    "symbol": overall["symbol"],
                    "timeframe": overall["timeframe"],
                    "params": overall["params"],
                    "profit": test_profit,
                    "max_drawdown": test_dd,
                    "trades": test_trades,
                }, jf, indent=2)

            trades_csv = os.path.join(cfg.out_dir, "steps", f"step_{step_id}_test_trades.csv")
            save_trades_csv(trades_csv, trades_list)

            # Summary row
            w.writerow([
                step_id,
                train_start.date().isoformat(), train_end.date().isoformat(),
                test_start.date().isoformat(), test_end.date().isoformat(),
                overall["symbol"], overall["timeframe"],
                f"{train_profit:.2f}", f"{train_dd:.2f}", train_trades,
                f"{test_profit:.2f}", f"{test_dd:.2f}", test_trades,
                json.dumps(overall["params"])
            ])
            f.flush()

        # Save stitched equity
        pd.DataFrame(stitched_equity).to_csv(stitched_csv, index=False)

    log_info(f"[WFO DONE] Summary: {summary_csv}")
    log_info(f"[WFO DONE] Stitched equity: {stitched_csv}")


if __name__ == "__main__":
    # Defaults based on config backtest dates; allow override via env
    default_start = str(BACKTEST_START_DATE) if "BACKTEST_START_DATE" in globals() else "2025-01-01"
    default_end = str(BACKTEST_END_DATE) if "BACKTEST_END_DATE" in globals() else "2025-06-01"

    cfg = WFOConfig(
        global_start=_parse_date_env("WFO_START", default_start),
        global_end=_parse_date_env("WFO_END", default_end),
        train_days=int(os.getenv("WFO_TRAIN_DAYS", "30")),
        test_days=int(os.getenv("WFO_TEST_DAYS", "7")),
        stride_days=int(os.getenv("WFO_STRIDE_DAYS", os.getenv("WFO_TEST_DAYS", "7"))),
        out_dir=os.getenv("WFO_OUT_DIR", "results/wfo_live_like"),
        score_mode=os.getenv("WFO_SCORE_MODE", "profit_minus_dd"),
        dd_weight=float(os.getenv("WFO_DD_WEIGHT", "1.5")),
        max_steps=int(os.getenv("WFO_MAX_STEPS", "0")),
    )

    run_wfo(cfg)
