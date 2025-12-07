import os
import json
import time as time_module
from datetime import datetime, timedelta, time as dtime
import pandas as pd

from config import *
from utils import log_info, log_error
from strategy import calculate_indicators
from mt5_connector import (
    initialize_mt5,
    shutdown_mt5,
    fetch_historical_data,
    execute_trade,
    adjust_trailing_stop,
)
from news_filters import (
    build_news_filters_for_backtest,
    bar_blocked_by_news,
    NEWS_FILTERS_VERSION,
)

# ---------------- helper config safe ---------------- #

def _get_cfg(name, default):
    try:
        from config import __dict__ as cdict  # type: ignore
        return cdict.get(name, default)
    except Exception:
        return default

TRADE_FREQUENCY_SECONDS = int(_get_cfg("TRADE_FREQUENCY_SECONDS", 30))
USE_NEWS_FILTER_SAFE    = bool(_get_cfg("USE_NEWS_FILTER", True))
HISTORY_BARS_LIVE       = int(_get_cfg("HISTORY_BARS_LIVE", 500))
BARS_FOR_ATR            = int(_get_cfg("BARS_FOR_ATR", 500))

ALLOWED_SESSIONS_SAFE = _get_cfg("ALLOWED_SESSIONS", [(dtime(8, 0), dtime(18, 0))])
WEEKEND_DAYS_SAFE     = _get_cfg("WEEKEND_DAYS", [5, 6])  # 5=sâmbătă, 6=duminică


def in_session(ts: datetime) -> bool:
    if ts.weekday() in WEEKEND_DAYS_SAFE:
        return False
    t = ts.time()
    for start, end in ALLOWED_SESSIONS_SAFE:
        if start <= t <= end:
            return True
    return False

# ---------------- load best_params.json ---------------- #

def load_best_params():
    candidates = [
        os.getenv("BEST_PARAMS_PATH", "../Backtester/results/best_params.json"),
        "results/best_params.json",
    ]
    for path in candidates:
        try:
            if os.path.exists(path):
                with open(path, "r") as f:
                    data = json.load(f)
                log_info(f"[AUTO MODE] Loaded best_params from {path}")
                return data
        except Exception as e:
            log_error(f"[AUTO MODE] Failed to load {path}: {e}")
    raise FileNotFoundError("best_params.json not found in expected locations")


def pick_adx_rsi_cols(df: pd.DataFrame):
    adx_col = None
    rsi_col = None
    for c in df.columns:
        cu = c.upper()
        if adx_col is None and cu.startswith("ADX"):
            adx_col = c
        if rsi_col is None and cu.startswith("RSI"):
            rsi_col = c
    return adx_col, rsi_col

# ---------------- main trading logic ---------------- #

def main():
    # MT5 connect
    if not initialize_mt5():
        log_error("MT5 initialization failed.")
        return

    # load params
    try:
        best = load_best_params()
    except Exception as e:
        log_error(f"[AUTO MODE] Cannot start: {e}")
        shutdown_mt5()
        return

    symbol    = best.get("symbol")
    timeframe = best.get("timeframe")
    params    = best.get("params", {})

    log_info(f"[AUTO MODE] Trading {symbol}@{timeframe} with params: {params}")

    # NEWS: construim ferestre pentru azi +/- 3 zile (ca să prindă tot)
    full_days = set()
    pauses    = {}
    if USE_NEWS_FILTER_SAFE:
        today = datetime.utcnow().date()
        start_dt = datetime.combine(today - timedelta(days=1), dtime(0, 0))
        end_dt   = datetime.combine(today + timedelta(days=3), dtime(23, 59))
        log_info(f"[NEWS] Building live windows ({NEWS_FILTERS_VERSION}) for "
                 f"{start_dt.date()} → {end_dt.date()}")
        full_days, pauses = build_news_filters_for_backtest(start_dt, end_dt)
        log_info(f"[NEWS] full_days={len(full_days)}, pause_days={len(pauses)}")
    else:
        log_info("[NEWS] Filter disabled in config")

    last_bar_time = None

    try:
        while True:
            now = datetime.utcnow()

            # weekend -> nu mai facem nimic
            if now.weekday() in WEEKEND_DAYS_SAFE:
                time_module.sleep(TRADE_FREQUENCY_SECONDS)
                continue

            # în afara sesiunii -> doar așteptăm
            if not in_session(now):
                time.sleep(TRADE_FREQUENCY_SECONDS)
                continue

            # verificăm dacă ziua e full blackout (FOMC, CPI, etc.)
            if USE_NEWS_FILTER_SAFE and now.date().isoformat() in full_days:
                log_info(f"[NEWS] Full-day blackout today ({now.date()}); exiting bot.")
                break

            # luăm istoricul
            df = fetch_historical_data(symbol, timeframe, HISTORY_BARS_LIVE)
            if df is None or df.empty:
                log_error("Empty DataFrame in live; sleeping...")
                time.sleep(TRADE_FREQUENCY_SECONDS)
                continue

            # asigură index de timp
            if not isinstance(df.index, pd.DatetimeIndex):
                if "time" in df.columns:
                    df["time"] = pd.to_datetime(df["time"])
                    df.set_index("time", inplace=True)
                else:
                    log_error("DataFrame has no DatetimeIndex and no 'time' column")
                    time.sleep(TRADE_FREQUENCY_SECONDS)
                    continue

            df = calculate_indicators(df, params)
            if df is None or df.empty:
                time.sleep(TRADE_FREQUENCY_SECONDS)
                continue

            # din nou, asigură index
            if not isinstance(df.index, pd.DatetimeIndex):
                if "time" in df.columns:
                    df["time"] = pd.to_datetime(df["time"])
                    df.set_index("time", inplace=True)
                else:
                    time.sleep(TRADE_FREQUENCY_SECONDS)
                    continue

            if len(df) < 2:
                time.sleep(TRADE_FREQUENCY_SECONDS)
                continue

            cur_ts = df.index[-1].to_pydatetime()

            # nu repetăm semnalul pe același bar
            if last_bar_time is not None and cur_ts <= last_bar_time:
                adjust_trailing_stop()  # trailing rulează oricum
                time.sleep(TRADE_FREQUENCY_SECONDS)
                continue

            last_bar_time = cur_ts

            cur  = df.iloc[-1]
            prev = df.iloc[-2]

            # skip weekend / sesiune încă o dată, dar pe baza barului
            if cur_ts.weekday() in WEEKEND_DAYS_SAFE or not in_session(cur_ts):
                adjust_trailing_stop()
                time.sleep(TRADE_FREQUENCY_SECONDS)
                continue

            # news window (pauses)
            if USE_NEWS_FILTER_SAFE and bar_blocked_by_news(cur_ts, full_days, pauses):
                log_info(f"[NEWS] Entries blocked at {cur_ts} (news window).")
                adjust_trailing_stop()
                time.sleep(TRADE_FREQUENCY_SECONDS)
                continue

            # determinăm coloane ADX / RSI (numele pot varia)
            adx_col, rsi_col = pick_adx_rsi_cols(df)
            if adx_col is None or rsi_col is None:
                log_error("ADX/RSI columns not found in live DataFrame.")
                adjust_trailing_stop()
                time.sleep(TRADE_FREQUENCY_SECONDS)
                continue

            # validare NaN
            if any(pd.isna([
                cur.get("supertrend_signal"),
                prev.get("supertrend_signal"),
                cur.get(adx_col),
                cur.get(rsi_col),
                cur.get("close"),
            ])):
                adjust_trailing_stop()
                time.sleep(TRADE_FREQUENCY_SECONDS)
                continue

            sig_cur  = cur["supertrend_signal"]
            sig_prev = prev["supertrend_signal"]
            adx_val  = float(cur[adx_col])
            rsi_val  = float(cur[rsi_col])
            price    = float(cur["close"])

            adx_ok = adx_val >= params.get("adx_threshold", 20)
            rsi_ok = (
                params.get("rsi_oversold", 30)
                <= rsi_val
                <= params.get("rsi_overbought", 70)
            )

            enter_long  = (sig_cur == "buy"  and sig_prev == "buy"  and adx_ok and rsi_ok)
            enter_short = (sig_cur == "sell" and sig_prev == "sell" and adx_ok and rsi_ok)

            log_info(
                f"[LIVE] time={cur_ts}, sig={sig_cur}, ADX={adx_val:.2f}, "
                f"RSI={rsi_val:.2f}, enter_long={enter_long}, enter_short={enter_short}"
            )

            if enter_long:
                execute_trade(symbol, "buy", price, timeframe, BARS_FOR_ATR)
            elif enter_short:
                execute_trade(symbol, "sell", price, timeframe, BARS_FOR_ATR)

            # trailing stop – distanțele sunt în comment, deci putem apela simplu
            adjust_trailing_stop()

            time_module.sleep(TRADE_FREQUENCY_SECONDS)

    finally:
        shutdown_mt5()


if __name__ == "__main__":
    main()
