import os
import json
import pandas as pd
import MetaTrader5 as mt5
from datetime import datetime, date, timedelta
import time as pytime  # avoid shadowing by datetime.time

from config import (
    USE_MANUAL_SYMBOL, MANUAL_SYMBOL, MANUAL_TIMEFRAME, MANUAL_PARAMS,
    LOT_SIZE, TRADE_FREQUENCY_SECONDS, FUNDED_MODE, Bars,
    USE_NEWS_FILTER, NEWS_LOOKAHEAD_DAYS, apply_news_env_from_config,
    CLOSE_HOUR,  # close-at time (local)
)
from mt5_connector import (
    initialize_mt5, shutdown_mt5, fetch_historical_data,
    execute_trade, adjust_trailing_stop
)
from strategy import calculate_indicators
from funded_risk import DailyLossManager
from utils import log_info, log_error

# ─── News filter (same module as backtester) ─────────────────────────────────
from news_filters import (
    build_news_filters_for_backtest,
    bar_blocked_by_news,
    NEWS_FILTERS_VERSION,
)

apply_news_env_from_config()  # ensure env for provider


# --- robust path resolver for best_params.json ---
def _resolve_best_params_path() -> str:
    base_dir   = os.path.dirname(os.path.abspath(__file__))                 # .../Live Trading
    repo_root  = os.path.abspath(os.path.join(base_dir, ".."))              # .../Tradingbot_V4
    candidates = [
        os.path.join(base_dir, "results", "best_params.json"),               # 1) Live Trading/results/
        os.path.join(repo_root, "Backtester", "results", "best_params.json"),# 2) Backtester/results/
        os.path.join(repo_root, "results", "best_params.json"),              # 3) project root /results/
    ]
    for p in candidates:
        p_norm = os.path.normpath(p)
        if os.path.exists(p_norm):
            return p_norm
    # fall back to the most likely one (Backtester/results)
    return os.path.normpath(os.path.join(repo_root, "Backtester", "results", "best_params.json"))


def build_live_news_windows(lookahead_days=7):
    """Build news windows from today 00:00 to today+lookahead 23:59:59."""
    start_dt = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    end_dt   = (start_dt + timedelta(days=lookahead_days)).replace(hour=23, minute=59, second=59, microsecond=0)
    log_info(f"[NEWS] Building live windows ({NEWS_FILTERS_VERSION}) for {start_dt.date()} → {end_dt.date()}")
    full_days, pauses = build_news_filters_for_backtest(start_dt, end_dt)
    return set(full_days or []), (pauses or {})


def _today_is_full_blackout(full_days: set) -> bool:
    """Return True if *today* is a full-day skip according to news rules."""
    try:
        return date.today().isoformat() in (full_days or set())
    except Exception:
        return False


def _close_all_open_positions(symbol: str | None = None, max_attempts: int = 3) -> None:
    """
    Close all open positions (optionally for a single symbol) before shutdown.
    Uses opposite market orders with correct bid/ask and broker's filling mode.
    """
    if not mt5.initialize():
        log_error("[CLOSE] MT5 not initialized; cannot close positions.")
        return

    positions = mt5.positions_get(symbol=symbol) if symbol else mt5.positions_get()
    if positions is None:
        log_error(f"[CLOSE] positions_get returned None: {mt5.last_error()}")
        return
    if len(positions) == 0:
        log_info("[CLOSE] No open positions to close.")
        return

    # Group by symbol to use correct filling/step per instrument
    by_symbol = {}
    for p in positions:
        by_symbol.setdefault(p.symbol, []).append(p)

    for sym, pos_list in by_symbol.items():
        si = mt5.symbol_info(sym)
        if not si:
            log_error(f"[CLOSE] No symbol info for {sym}")
            continue
        if not si.visible:
            mt5.symbol_select(sym, True)

        # choose a safe filling mode
        filling = si.filling_mode
        if filling not in (mt5.ORDER_FILLING_FOK, mt5.ORDER_FILLING_IOC, mt5.ORDER_FILLING_RETURN):
            filling = mt5.ORDER_FILLING_IOC

        step = si.volume_step or 0.01
        for p in pos_list:
            side = "sell" if p.type == mt5.POSITION_TYPE_BUY else "buy"
            remaining = p.volume

            attempts = 0
            while remaining > 0 and attempts < max_attempts:
                attempts += 1
                tick = mt5.symbol_info_tick(sym)
                if not tick:
                    log_error(f"[CLOSE] No tick for {sym}")
                    break

                price = tick.bid if side == "sell" else tick.ask
                vol = round(remaining / step) * step
                vol = max(si.volume_min, min(vol, si.volume_max))

                req = {
                    "action": mt5.TRADE_ACTION_DEAL,
                    "symbol": sym,
                    "type": mt5.ORDER_TYPE_SELL if side == "sell" else mt5.ORDER_TYPE_BUY,
                    "price": price,
                    "volume": vol,
                    "deviation": 20,
                    "type_filling": filling,
                    "type_time": mt5.ORDER_TIME_GTC,
                    "position": p.ticket,  # link to position (helps on hedging)
                }
                res = mt5.order_send(req)
                if res and res.retcode == mt5.TRADE_RETCODE_DONE:
                    log_info(f"[CLOSE] Closed {vol} {sym} ({'BUY' if p.type==0 else 'SELL'} → {side})")
                    remaining = round(remaining - vol, 8)
                else:
                    rc = getattr(res, "retcode", None)
                    log_error(f"[CLOSE] Close failed {sym} vol={vol} retcode={rc} last_error={mt5.last_error()}")
                    # try alternate fillings quickly
                    for alt in (mt5.ORDER_FILLING_IOC, mt5.ORDER_FILLING_FOK, mt5.ORDER_FILLING_RETURN):
                        if alt == filling:
                            continue
                        req["type_filling"] = alt
                        res2 = mt5.order_send(req)
                        if res2 and res2.retcode == mt5.TRADE_RETCODE_DONE:
                            log_info(f"[CLOSE] Closed {vol} {sym} with alt filling")
                            remaining = round(remaining - vol, 8)
                            break
                    else:
                        # could not close this attempt
                        pytime.sleep(0.5)

            if remaining > 0:
                log_error(f"[CLOSE] Could not fully close position {p.ticket} on {sym}. Remaining {remaining}")


# Initialize news windows once
news_full_days, news_pauses = (set(), {})
if USE_NEWS_FILTER:
    try:
        news_full_days, news_pauses = build_live_news_windows(NEWS_LOOKAHEAD_DAYS)
        log_info(f"[NEWS] full_days={len(news_full_days)}, pause_days={len(news_pauses)}")
    except Exception as e:
        log_error(f"[NEWS] Failed to initialize news filter: {e}")
        news_full_days, news_pauses = set(), {}

# ─── INITIALIZE & LOGIN ──────────────────────────────────────────────────────
if not initialize_mt5():
    log_error("MT5 init failed. Exiting.")
    exit()

# If today is a full-day blackout, shut down immediately (close positions first)
if USE_NEWS_FILTER and _today_is_full_blackout(news_full_days):
    log_info("[NEWS] Today is a FULL-DAY blackout (e.g., FOMC/CPI/GDP/BoE/Holiday). Closing positions and exiting.")
    _close_all_open_positions()  # close everything
    shutdown_mt5()
    exit()

# choose manual vs. auto
def load_best_config():
    path = _resolve_best_params_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data["symbol"], data["timeframe"], data["params"]
    except Exception as e:
        log_error(f"Failed to load best_params.json at {path}: {e}")
        return None, None, None

if USE_MANUAL_SYMBOL:
    symbol = MANUAL_SYMBOL
    timeframe = MANUAL_TIMEFRAME
    best_params = MANUAL_PARAMS
    log_info(f"[MANUAL MODE] Trading {symbol}@{timeframe}")
else:
    symbol, timeframe, best_params = load_best_config()
    if not all([symbol, timeframe, best_params]):
        log_error("Missing best_params.json. Exiting.")
        shutdown_mt5()
        exit()
    log_info(f"[AUTO MODE] Trading {symbol}@{timeframe} with loaded params.")

daily_loss = DailyLossManager()
if FUNDED_MODE and daily_loss.should_stop_bot():
    log_error("Daily loss limit already hit. Closing positions and stopping.")
    _close_all_open_positions()  # close everything
    shutdown_mt5()
    exit()

# Track the day to refresh calendar at midnight
_last_calendar_day = date.today()

# ─── MAIN LOOP ────────────────────────────────────────────────────────────────
while True:
    now = datetime.now()

    # --- HARD STOP AT CLOSE_HOUR (local machine time) ---
    today_close = now.replace(
        hour=CLOSE_HOUR.hour,
        minute=CLOSE_HOUR.minute,
        second=getattr(CLOSE_HOUR, "second", 0),
        microsecond=0
    )
    if now >= today_close:
        log_info(f"[SCHEDULE] Reached CLOSE_HOUR ({CLOSE_HOUR}). Closing positions and shutting down.")
        _close_all_open_positions(symbol=None)  # close all symbols just in case
        shutdown_mt5()
        exit()

    # If today is a full-day blackout at any point, shut down (in case windows updated)
    if USE_NEWS_FILTER and _today_is_full_blackout(news_full_days):
        log_info("[NEWS] FULL-DAY blackout detected for today. Closing positions and exiting.")
        _close_all_open_positions()
        shutdown_mt5()
        exit()

    # Refresh news windows at midnight (once per new day)
    if USE_NEWS_FILTER and date.today() != _last_calendar_day:
        try:
            news_full_days, news_pauses = build_live_news_windows(NEWS_LOOKAHEAD_DAYS)
            _last_calendar_day = date.today()
            log_info(f"[NEWS] Calendar refreshed: full_days={len(news_full_days)}, pause_days={len(news_pauses)}")
            # Immediately shut down if the new day is a full-day blackout
            if _today_is_full_blackout(news_full_days):
                log_info("[NEWS] New day is FULL-DAY blackout. Closing positions and exiting.")
                _close_all_open_positions()
                shutdown_mt5()
                exit()
        except Exception as e:
            log_error(f"[NEWS] Calendar refresh failed: {e}")

    # Entries-only block for pause windows (NOT full-day; we already exit for full-day)
    entries_blocked = False
    if USE_NEWS_FILTER:
        try:
            # For pauses only: we still evaluate trailing/exits while blocking entries
            entries_blocked = bar_blocked_by_news(now, set(), news_pauses)
            if entries_blocked:
                log_info(f"[NEWS] Entries blocked at {now} (pause window).")
        except Exception as e:
            log_error(f"[NEWS] bar check failed: {e}")
            entries_blocked = False

    # 1) Daily‐loss check
    if FUNDED_MODE:
        daily_loss.update_day()
        if daily_loss.should_stop_bot():
            log_error("Daily loss exceeded. Closing positions and stopping.")
            _close_all_open_positions()
            shutdown_mt5()
            exit()

    # 2) Fetch & process market data
    df = fetch_historical_data(symbol, timeframe, Bars)
    if df.empty:
        log_error("No data, retrying...")
        pytime.sleep(TRADE_FREQUENCY_SECONDS)
        continue

    df = calculate_indicators(df, best_params)

    # Safety: need at least 2 bars
    if len(df) < 2:
        pytime.sleep(TRADE_FREQUENCY_SECONDS)
        continue

    last, prev = df.iloc[-1], df.iloc[-2]
    signal = last["supertrend_signal"] if last["supertrend_signal"] == prev["supertrend_signal"] else "hold"
    adx, rsi, price = last["adx"], last["rsi"], last["close"]
    log_info(f"Signal={signal}, ADX={adx:.2f}, RSI={rsi:.2f}, Price={price}")

    # 3) Entry logic (blocked by news pause windows)
    if not entries_blocked:
        if signal == "buy" and adx >= best_params["adx_threshold"] and best_params["rsi_oversold"] <= rsi <= best_params["rsi_overbought"]:
            execute_trade(symbol, "buy", price, timeframe, Bars)
        elif signal == "sell" and adx >= best_params["adx_threshold"] and best_params["rsi_oversold"] <= rsi <= best_params["rsi_overbought"]:
            execute_trade(symbol, "sell", price, timeframe, Bars)
    else:
        log_info("[NEWS] Skipping new entries this bar due to news PAUSE window.")

    # 4) Trailing stop management (still runs during pauses)
    try:
        adjust_trailing_stop()  # keep your original signature/behavior
    except TypeError:
        pass

    # 5) Wait until next iteration
    pytime.sleep(TRADE_FREQUENCY_SECONDS)
