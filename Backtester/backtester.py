# backtester.py — ATR-based SL/TRAIL + optional risk-based lot sizing
# Signals on TF bars; fills on next tick (Bid/Ask). Ticks are cached on disk.
# Stage-1 ranks indicators using fixed ATR-multipliers; Stage-2 tunes ATR multipliers on Top-3.
# News gating for entries & exits. Fixed-setup mode logs Excel/CSV trades.
# Version: 4.0.1 — risk-decoupled-from-SL, consistent scaling on flips

import os
import json
from datetime import datetime, timedelta
from itertools import product
from multiprocessing import Pool, cpu_count

import pandas as pd
import numpy as np
import MetaTrader5 as mt5

from config import *
from utils import log_info, log_error
from mt5_connector import initialize_mt5, shutdown_mt5, fetch_historical_data
from strategy import calculate_indicators
from news_filters import build_news_filters_for_backtest, bar_blocked_by_news, NEWS_FILTERS_VERSION


# -------------------- helpers & safe config getters --------------------

def _get_cfg(name, default):
    try:
        from config import __dict__ as cdict  # type: ignore
        return cdict.get(name, default)
    except Exception:
        return default

ENABLE_RISK   = bool(_get_cfg("ENABLE_RISK_SIZING", False))
RISK_PCT      = float(_get_cfg("RISK_PER_TRADE", 0.005))       # 0.5% default if missing
BASE_BALANCE  = float(_get_cfg("BASE_BALANCE", 10000.0))       # for SL/TRAIL scaling (optional)
RISK_EXPONENT = float(_get_cfg("RISK_EXPONENT", 0.0))          # 0=off; 1=proportional

def worker_init():
    if not mt5.initialize():
        log_error("MT5 initialization failed in worker")

def pip_size(symbol: str) -> float:
    info = mt5.symbol_info(symbol)
    if not info:
        return 0.0001
    return 0.01 if info.digits in (2, 3) else 0.0001

def is_session_allowed(ts: datetime) -> bool:
    if ts.weekday() in WEEKEND_DAYS:
        return False
    t = ts.time()
    for start, end in ALLOWED_SESSIONS:
        if start <= t <= end:
            return True
    return False

def _round_trip_commission(lots: float) -> float:
    return 2.0 * COMMISSION_PER_LOT_SAFE() * float(lots)

def COMMISSION_PER_LOT_SAFE():
    try:
        return float(COMMISSION_PER_LOT)
    except Exception:
        return 0.0

def _ensure_dirs():
    os.makedirs("results", exist_ok=True)
    os.makedirs("results/tick_cache", exist_ok=True)

def _to_excel_or_csv(df: pd.DataFrame, path_xlsx: str, path_csv: str):
    try:
        import openpyxl  # noqa
        df.to_excel(path_xlsx, index=False)
        log_info(f"[TRADES] Saved Excel: {path_xlsx}")
    except Exception as e:
        log_error(f"[TRADES] Excel write failed ({e}); saving CSV fallback.")
        df.to_csv(path_csv, index=False)
        log_info(f"[TRADES] Saved CSV: {path_csv}")


# -------------------- tick cache --------------------

def _tick_cache_path(symbol: str, start_dt: datetime, end_dt: datetime) -> str:
    s = pd.to_datetime(start_dt).strftime("%Y%m%d_%H%M%S")
    e = pd.to_datetime(end_dt).strftime("%Y%m%d_%H%M%S")
    return f"results/tick_cache/{symbol}_{s}_{e}"

def _load_ticks_cached(symbol: str, start_dt: datetime, end_dt: datetime) -> pd.DataFrame:
    base = _tick_cache_path(symbol, start_dt, end_dt)
    pq = base + ".parquet"
    csv = base + ".csv"
    try:
        if os.path.exists(pq):
            df = pd.read_parquet(pq)
            df.index = pd.to_datetime(df.index)
            return df.sort_index()
    except Exception:
        pass
    try:
        if os.path.exists(csv):
            df = pd.read_csv(csv, parse_dates=["time"])
            df.set_index("time", inplace=True)
            return df.sort_index()
    except Exception:
        pass
    return pd.DataFrame()

def _save_ticks_cached(df: pd.DataFrame, symbol: str, start_dt: datetime, end_dt: datetime):
    if df is None or df.empty:
        return
    base = _tick_cache_path(symbol, start_dt, end_dt)
    pq = base + ".parquet"
    csv = base + ".csv"
    try:
        df.to_parquet(pq, index=True)
        log_info(f"[TICKS] Cached: {pq}")
    except Exception as e:
        log_error(f"[TICKS] Parquet cache failed ({e}); falling back to CSV.")
        df.reset_index().rename(columns={"index": "time"}).to_csv(csv, index=False)
        log_info(f"[TICKS] Cached: {csv}")

def fetch_ticks(symbol: str, start_dt: datetime, end_dt: datetime) -> pd.DataFrame:
    cached = _load_ticks_cached(symbol, start_dt, end_dt)
    if not cached.empty:
        return cached
    ticks = mt5.copy_ticks_range(symbol, start_dt, end_dt, mt5.COPY_TICKS_ALL)
    if ticks is None or len(ticks) == 0:
        return pd.DataFrame()
    df = pd.DataFrame(ticks)
    if "time" not in df.columns or "bid" not in df.columns or "ask" not in df.columns:
        return pd.DataFrame()
    df["time"] = pd.to_datetime(df["time"], unit="s")
    df = df[["time", "bid", "ask"]].dropna()
    df.set_index("time", inplace=True)
    df.sort_index(inplace=True)
    _save_ticks_cached(df, symbol, start_dt, end_dt)
    return df


# -------------------- ATR utilities --------------------

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

def quantize_lot(symbol_info, lots: float) -> float:
    if symbol_info is None:
        return max(0.01, round(lots, 2))
    step = symbol_info.volume_step or 0.01
    vmin = symbol_info.volume_min or 0.01
    vmax = symbol_info.volume_max or 100.0
    q = max(vmin, min(vmax, round(lots / step) * step))
    return float(q)


# -------------------- intrabar fallback from bars --------------------

def m1_path_points(row: pd.Series) -> list:
    """Approximate within-bar path for SL/trailing if no ticks. Returns [(time, bid, ask)]."""
    t = row.name
    o = float(row["open"]); h = float(row["high"])
    l = float(row["low"]);  c = float(row["close"])
    spread_pts = float(row.get("spread", 0.0))
    sym = row.get("symbol", "")
    info = mt5.symbol_info(sym) if sym else None
    point = info.point if info else 0.0001

    def b_a(mid: float):
        ask = mid + spread_pts * point
        bid = mid
        return bid, ask

    dt0 = t
    dt1 = t + timedelta(seconds=15)
    dt2 = t + timedelta(seconds=30)
    dt3 = t + timedelta(seconds=45)

    seq = []
    if c > o + 1e-12:
        mids = [(dt0, o), (dt1, l), (dt2, h), (dt3, c)]
    elif c < o - 1e-12:
        mids = [(dt0, o), (dt1, h), (dt2, l), (dt3, c)]
    else:
        mids = [(dt0, o), (dt1, h), (dt2, l), (dt3, c)]
    for tm, mid in mids:
        bid, ask = b_a(mid)
        seq.append((tm, bid, ask))
    return seq


# -------------------- core simulator --------------------

def simulate_params(task):
    """
    Signals on TF bars; fills on next tick (Bid/Ask).
    ATR-based distances (k_sl/k_trg/k_dst) locked at entry bar's ATR.
    Optional risk-based lot sizing per trade.
    """
    symbol, timeframe, params, full_days, pauses = task

    if not mt5.initialize():
        log_error("MT5 init failed in worker")
        return {"profit": 0.0, "symbol": symbol, "timeframe": timeframe, "params": params}

    # bars + indicators
    df = fetch_historical_data(symbol, timeframe, BACKTEST_START_DATE, BACKTEST_END_DATE)
    if df.empty:
        return {"profit": 0.0, "symbol": symbol, "timeframe": timeframe, "params": params}

    df["symbol"] = symbol
    df = calculate_indicators(df, params)  # expects supertrend_signal, adx, rsi...
    if df.empty:
        return {"profit": 0.0, "symbol": symbol, "timeframe": timeframe, "params": params}

    if not isinstance(df.index, pd.DatetimeIndex):
        if "time" in df.columns:
            df["time"] = pd.to_datetime(df["time"])
            df.set_index("time", inplace=True)
        else:
            log_error("DataFrame missing time index.")
            return {"profit": 0.0, "symbol": symbol, "timeframe": timeframe, "params": params}

    info = mt5.symbol_info(symbol)
    if info is None:
        log_error(f"No symbol info for {symbol}")
        return {"profit": 0.0, "symbol": symbol, "timeframe": timeframe, "params": params}

    point = info.point
    pip   = pip_size(symbol)
    contract_size = info.trade_contract_size

    # ATR series (price -> points)
    atr_period = int(params.get("atr_period", params.get("supertrend_period", 14)))
    atr_price = compute_atr_series(df, atr_period)
    atr_pts_series = (atr_price / point).ffill()
    df["atr_pts"] = atr_pts_series

    # ticks
    start_dt = pd.to_datetime(BACKTEST_START_DATE).to_pydatetime()
    end_dt   = pd.to_datetime(BACKTEST_END_DATE).to_pydatetime()
    ticks = fetch_ticks(symbol, start_dt, end_dt)

    def next_tick_after(ts: pd.Timestamp):
        if ticks.empty:
            return None
        idx = ticks.index.searchsorted(ts + pd.Timedelta(microseconds=1), side="left")
        if idx >= len(ticks):
            return None
        tt = ticks.iloc[idx]
        return (ticks.index[idx], float(tt["bid"]), float(tt["ask"]))

    def ticks_between(t0: pd.Timestamp, t1: pd.Timestamp):
        if ticks.empty:
            return []
        sl = ticks.loc[(ticks.index > t0) & (ticks.index <= t1)]
        if sl.empty:
            return []
        return [(ti, float(r.bid), float(r.ask)) for ti, r in sl.iterrows()]

    # state
    balance     = START_BALANCE
    side        = 0
    entry_price = 0.0
    entry_time  = None
    sl_price    = None
    lots_open   = 0.0

    trades_log = []

    # -------------------------------- main loop --------------------------------
    for i in range(1, len(df)):
        row  = df.iloc[i]
        prev = df.iloc[i - 1]
        ts   = df.index[i]
        ts_prev = df.index[i - 1]

        # session/news gating
        if ts.weekday() in WEEKEND_DAYS or not is_session_allowed(ts):
            continue
        if USE_NEWS_FILTER and bar_blocked_by_news(ts.to_pydatetime(), full_days, pauses):
            continue

        # manage open position on intrabar ticks
        if side != 0:
            intraticks = ticks_between(ts_prev, ts)
            iter_seq = intraticks if intraticks else m1_path_points(row)
            for t_idx, bid, ask in iter_seq:
                if not (ts_prev < t_idx <= ts):
                    continue
                if side == 1:
                    # trailing trigger already locked at entry distances
                    if trig_d > 0.0 and dist_d > 0.0:
                        move = (bid - entry_price)
                        if move >= trig_d:
                            sl_price = max(sl_price or -1e9, bid - dist_d)
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
                        side = 0; entry_price = 0.0; entry_time = None; sl_price = None; lots_open = 0.0
                        break
                else:
                    if trig_d > 0.0 and dist_d > 0.0:
                        move = (entry_price - ask)
                        if move >= trig_d:
                            sl_price = min(sl_price or 1e9, ask + dist_d)
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
                        side = 0; entry_price = 0.0; entry_time = None; sl_price = None; lots_open = 0.0
                        break

        # bar-close signal evaluation
        sig_prev = str(prev["supertrend_signal"])
        sig_cur = str(row["supertrend_signal"])

        # --- safe lookup for ADX and RSI ---
        adx_cols = [c for c in df.columns if c.upper().startswith("ADX_")]
        adx = float(row[adx_cols[0]]) if adx_cols else np.nan

        rsi_cols = [c for c in df.columns if c.upper().startswith("RSI_")]
        rsi = float(row[rsi_cols[0]]) if rsi_cols else np.nan

        adx_ok = adx >= float(params["adx_threshold"])
        rsi_ok = float(params["rsi_oversold"]) <= rsi <= float(params["rsi_overbought"])

        enter_long  = (sig_prev == "buy"  and sig_cur == "buy"  and adx_ok and rsi_ok)
        enter_short = (sig_prev == "sell" and sig_cur == "sell" and adx_ok and rsi_ok)

        # fill next tick (or synth from bar)
        nxt = next_tick_after(ts)
        if nxt is None:
            spread_pts = float(row.get("spread", 0.0))
            ask = float(row["close"]) + spread_pts * point
            bid = float(row["close"])
            t_fill = ts + timedelta(microseconds=1)
        else:
            t_fill, bid, ask = nxt

        # ---------------- flips ----------------
        if side == 1 and enter_short:
            # close long
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

            # open short
            atr_pts = float(row["atr_pts"])
            # SL/TRAIL scale by account size via BASE_BALANCE/RISK_EXPONENT, NOT by RISK_PCT
            risk_scale = (balance / BASE_BALANCE) ** RISK_EXPONENT if BASE_BALANCE > 0 else 1.0
            k_sl   = float(params["k_sl"]);  k_trg  = float(params["k_trg"]);  k_dist = float(params["k_dist"])
            sl_d   = k_sl * atr_pts * point * risk_scale
            trig_d = k_trg * atr_pts * point * risk_scale
            dist_d = k_dist * atr_pts * point * risk_scale

            lots = float(LOT_SIZE)
            if ENABLE_RISK:
                risk_cash = balance * RISK_PCT
                denom = max(1e-9, sl_d * contract_size)
                lots = quantize_lot(info, risk_cash / denom) if denom > 0 else float(LOT_SIZE)
                if lots <= 0.0:
                    lots = float(LOT_SIZE)

            side = -1
            entry_price = bid
            entry_time  = t_fill
            sl_price = entry_price + sl_d if sl_d > 0 else None
            lots_open = lots
            continue

        if side == -1 and enter_long:
            # close short
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

            # open long
            atr_pts = float(row["atr_pts"])
            risk_scale = (balance / BASE_BALANCE) ** RISK_EXPONENT if BASE_BALANCE > 0 else 1.0
            k_sl   = float(params["k_sl"]);  k_trg  = float(params["k_trg"]);  k_dist = float(params["k_dist"])
            sl_d   = k_sl * atr_pts * point * risk_scale
            trig_d = k_trg * atr_pts * point * risk_scale
            dist_d = k_dist * atr_pts * point * risk_scale

            lots = float(LOT_SIZE)
            if ENABLE_RISK:
                risk_cash = balance * RISK_PCT
                denom = max(1e-9, sl_d * contract_size)
                lots = quantize_lot(info, risk_cash / denom) if denom > 0 else float(LOT_SIZE)
                if lots <= 0.0:
                    lots = float(LOT_SIZE)

            side = 1
            entry_price = ask
            entry_time  = t_fill
            sl_price = entry_price - sl_d if sl_d > 0 else None
            lots_open = lots
            continue

        # ---------------- fresh entries ----------------
        if side == 0 and (enter_long or enter_short):
            atr_pts = float(row["atr_pts"])
            risk_scale = (balance / BASE_BALANCE) ** RISK_EXPONENT if BASE_BALANCE > 0 else 1.0
            k_sl   = float(params["k_sl"]);  k_trg  = float(params["k_trg"]);  k_dist = float(params["k_dist"])
            sl_d   = k_sl * atr_pts * point * risk_scale
            trig_d = k_trg * atr_pts * point * risk_scale
            dist_d = k_dist * atr_pts * point * risk_scale

            lots = float(LOT_SIZE)
            if ENABLE_RISK:
                risk_cash = balance * RISK_PCT
                denom = max(1e-9, sl_d * contract_size)
                lots = quantize_lot(info, risk_cash / denom) if denom > 0 else float(LOT_SIZE)
                if lots <= 0.0:
                    lots = float(LOT_SIZE)

            if enter_long:
                side = 1
                entry_price = ask
                entry_time  = t_fill
                sl_price = entry_price - sl_d if sl_d > 0 else None
                lots_open = lots
            else:
                side = -1
                entry_price = bid
                entry_time  = t_fill
                sl_price = entry_price + sl_d if sl_d > 0 else None
                lots_open = lots

    # force close at end if configured
    if FORCE_CLOSE_AT_END and side != 0 and entry_time is not None:
        last_bar_ts = df.index[-1]
        nxt = next_tick_after(last_bar_ts)
        if nxt is None:
            row = df.iloc[-1]
            spread_pts = float(row.get("spread", 0.0))
            ask = float(row["close"]) + spread_pts * point
            bid = float(row["close"])
            t_exit = last_bar_ts + timedelta(microseconds=1)
        else:
            t_exit, bid, ask = nxt

        if side == 1:
            pnl = (bid - entry_price) * contract_size * lots_open - _round_trip_commission(lots_open)
            trades_log.append({
                "open_time": entry_time, "close_time": t_exit,
                "symbol": symbol, "timeframe": timeframe, "side": "buy",
                "lots": lots_open, "entry_price": entry_price, "exit_price": bid,
                "commission_roundtrip": _round_trip_commission(lots_open),
                "profit": pnl, "pnl_pips": (bid - entry_price) / pip,
                "exit_reason": "session_end",
            })
            balance += pnl
        else:
            pnl = (entry_price - ask) * contract_size * lots_open - _round_trip_commission(lots_open)
            trades_log.append({
                "open_time": entry_time, "close_time": t_exit,
                "symbol": symbol, "timeframe": timeframe, "side": "sell",
                "lots": lots_open, "entry_price": entry_price, "exit_price": ask,
                "commission_roundtrip": _round_trip_commission(lots_open),
                "profit": pnl, "pnl_pips": (entry_price - ask) / pip,
                "exit_reason": "session_end",
            })
            balance += pnl

    mt5.shutdown()
    return {
        "profit": balance - START_BALANCE,
        "symbol": symbol, "timeframe": timeframe,
        "params": params, "trades": len(trades_log),
        "_trades": trades_log,
    }


# -------------------- Stage-1 / Stage-2 (ATR multipliers) --------------------

def stage1_scan(symbol, timeframe, full_days, pauses):
    """Stage-1: scan indicator params; fixed ATR multipliers (S1_K_*)."""
    stage1_tasks = []
    for atr_p, mult, adx_p, adx_th, rsi_p, rsi_lo, rsi_hi in product(
        range(5, 15), range(2, 6),
        range(10, 20, 5), range(20, 35, 5),
        range(10, 20, 5), range(25, 40, 5),
        range(60, 75, 5)
    ):
        params = {
            "supertrend_period": atr_p,
            "supertrend_multiplier": mult,
            "adx_period": adx_p,
            "adx_threshold": adx_th,
            "rsi_period": rsi_p,
            "rsi_oversold": rsi_lo,
            "rsi_overbought": rsi_hi,
            # Stage-1 ATR multipliers:
            "k_sl": S1_K_SL,
            "k_trg": S1_K_TRG,
            "k_dist": S1_K_DIST,
            "atr_period": atr_p,
        }
        stage1_tasks.append((symbol, timeframe, params, full_days, pauses))

    workers = max(1, min(MAX_WORKERS_HINT, cpu_count()))
    with Pool(workers, initializer=worker_init) as pool:
        results1 = pool.map(simulate_params, stage1_tasks)

    best1 = max(results1, key=lambda x: x["profit"])
    return best1["params"], best1["profit"]


def stage2_tune(symbol, timeframe, best_indicators, full_days, pauses):
    """Stage-2: tune ATR multipliers using best indicator set from Stage-1."""
    stage2_tasks = []
    for ksl in S2_K_SL_LIST:
        for ktrg in S2_K_TRG_LIST:
            for kdist in S2_K_DIST_LIST:
                p = best_indicators.copy()
                p.update({
                    "k_sl": ksl,
                    "k_trg": ktrg,
                    "k_dist": kdist,
                    "atr_period": best_indicators.get("atr_period", best_indicators.get("supertrend_period", 14)),
                })
                stage2_tasks.append((symbol, timeframe, p, full_days, pauses))

    workers = max(1, min(MAX_WORKERS_HINT, cpu_count()))
    with Pool(workers, initializer=worker_init) as pool:
        results2 = pool.map(simulate_params, stage2_tasks)

    best2 = max(results2, key=lambda x: x["profit"])
    return best2["params"], best2["profit"]


# -------------------- main --------------------

def main():
    _ensure_dirs()
    apply_news_env_from_config()
    if not initialize_mt5():
        raise SystemExit("MT5 login failed.")
    log_info(f"[NEWS] news_filters version: {NEWS_FILTERS_VERSION}")

    # Build news windows once
    if USE_NEWS_FILTER:
        start_dt = pd.to_datetime(BACKTEST_START_DATE).to_pydatetime()
        end_dt   = pd.to_datetime(BACKTEST_END_DATE).to_pydatetime()
        full_days, pauses = build_news_filters_for_backtest(start_dt, end_dt)
    else:
        full_days, pauses = set(), {}
    log_info(f"[NEWS] full_days={len(full_days)} days, pause_days={len(pauses)}")
    log_info(f"[RISK] ENABLE_RISK_SIZING={ENABLE_RISK}, RISK_PER_TRADE={RISK_PCT}, "
             f"BASE_BALANCE={BASE_BALANCE}, RISK_EXPONENT={RISK_EXPONENT}")

    # -------- Fixed-Setup mode (single run + trade report) --------
    if FIXED_PARAMS_MODE and FIXED_SETUP:
        symbol    = FIXED_SETUP["symbol"]
        timeframe = FIXED_SETUP["timeframe"]
        params    = FIXED_SETUP["params"].copy()

        # ensure ATR multipliers exist
        params.setdefault("k_sl", S1_K_SL)
        params.setdefault("k_trg", S1_K_TRG)
        params.setdefault("k_dist", S1_K_DIST)
        params.setdefault("atr_period", params.get("supertrend_period", 14))

        log_info(f"[MODE] Fixed-setup — {symbol}@{timeframe} params={params}")
        res = simulate_params((symbol, timeframe, params, full_days, pauses))

        out = {"best_profit": res["profit"], "symbol": symbol, "timeframe": timeframe, "params": params}
        with open("results/best_params.json", "w") as f:
            json.dump(out, f, indent=4)
        log_info(f"[DONE] Result: {out}")

        trades = res.get("_trades", [])
        if trades:
            tdf = pd.DataFrame(trades)
            base = f"results/trades_{symbol}_{timeframe}_{BACKTEST_START_DATE}_{BACKTEST_END_DATE}"
            _to_excel_or_csv(tdf, f"{base}.xlsx", f"{base}.csv")
        else:
            log_info("[TRADES] No closed trades to log.")

        shutdown_mt5()
        return

    # -------- Grid-search mode with Top-3 Stage-2 --------
    stage1_results = []
    for symbol in SYMBOL_LIST:
        for timeframe in TIMEFRAME_LIST:
            log_info(f"[STAGE 1] Scanning indicators for {symbol}@{timeframe} …")
            try:
                best_ind, p1 = stage1_scan(symbol, timeframe, full_days, pauses)
            except Exception as e:
                log_error(f"Stage-1 error {symbol}@{timeframe}: {e}")
                continue
            stage1_results.append({
                "symbol": symbol,
                "timeframe": timeframe,
                "best_indicators": best_ind,
                "profit1": p1
            })

    if not stage1_results:
        shutdown_mt5()
        raise SystemExit("No Stage-1 results produced.")

    stage1_results.sort(key=lambda r: r["profit1"], reverse=True)
    top_k = stage1_results[:TOP_K_STAGE2]
    log_info(f"[STAGE 1] Top-{TOP_K_STAGE2} → "
             f"{[ (r['symbol'], r['timeframe'], round(r['profit1'],2)) for r in top_k ]}")

    overall_best = {"best_profit": -float("inf"), "symbol": None, "timeframe": None, "params": None}

    for r in top_k:
        symbol = r["symbol"]; timeframe = r["timeframe"]; best_ind = r["best_indicators"]
        log_info(f"[STAGE 2] Tuning ATR multipliers for {symbol}@{timeframe} …")
        try:
            final_params, p2 = stage2_tune(symbol, timeframe, best_ind, full_days, pauses)
        except Exception as e:
            log_error(f"Stage-2 error {symbol}@{timeframe}: {e}")
            continue

        if p2 > overall_best["best_profit"]:
            overall_best.update({
                "best_profit": p2,
                "symbol": symbol,
                "timeframe": timeframe,
                "params": final_params
            })

    with open("results/best_params.json", "w") as f:
        json.dump(overall_best, f, indent=4)
    log_info(f"[DONE] Best result: {overall_best}")
    shutdown_mt5()


if __name__ == "__main__":
    main()
