import MetaTrader5 as mt5
import json
import os
import pandas as pd
import time as tmod

from config import *
from utils import log_info, log_error

# ----------------------------------------------------------------------
# Helper pt citire safe din config (ca în backtester)
# ----------------------------------------------------------------------

def _get_cfg(name, default):
    try:
        from config import __dict__ as cdict  # type: ignore
        return cdict.get(name, default)
    except Exception:
        return default

ENABLE_RISK_SIZING_SAFE = bool(_get_cfg("ENABLE_RISK_SIZING", False))
RISK_PER_TRADE_SAFE     = float(_get_cfg("RISK_PER_TRADE", 0.005))
BASE_BALANCE_SAFE       = float(_get_cfg("BASE_BALANCE", 10000.0))
RISK_EXPONENT_SAFE      = float(_get_cfg("RISK_EXPONENT", 0.0))

# ----------------------------------------------------------------------
# ATR + quantizare volum (logică identică cu backtesterul)
# ----------------------------------------------------------------------

def compute_atr_series(df: pd.DataFrame, period: int) -> pd.Series:
    """
    Wilder ATR în unități de preț (high/low/close).
    Identic cu backtesterul.
    """
    h = df["high"].astype(float)
    l = df["low"].astype(float)
    c = df["close"].astype(float)
    prev_c = c.shift(1)

    tr1 = h - l
    tr2 = (h - prev_c).abs()
    tr3 = (l - prev_c).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    atr = tr.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    return atr


def quantize_lot(symbol_info, lots: float) -> float:
    """
    Rotunjește volumul la pasul corect (volume_step),
    limitat între volume_min și volume_max.
    Identic cu backtesterul.
    """
    if symbol_info is None:
        return max(0.01, round(lots, 2))
    step = symbol_info.volume_step or 0.01
    vmin = symbol_info.volume_min or 0.01
    vmax = symbol_info.volume_max or 100.0
    q = max(vmin, min(vmax, round(lots / step) * step))
    return float(q)

# ----------------------------------------------------------------------
# Încărcare parametri din best_params.json generat de backtester
# ----------------------------------------------------------------------

_LIVE_PARAMS_CACHE = None

def _load_live_params():
    """
    Citește parametrii finali (inclusiv k_sl/k_trg/k_dist/atr_period)
    din best_params.json.
    """
    global _LIVE_PARAMS_CACHE
    if _LIVE_PARAMS_CACHE is not None:
        return _LIVE_PARAMS_CACHE

    candidates = [
        os.getenv("BEST_PARAMS_PATH", "../Backtester/results/best_params.json"),
        "results/best_params.json",
    ]

    for path in candidates:
        try:
            if os.path.exists(path):
                with open(path, "r") as f:
                    data = json.load(f)
                params = data.get("params", {})

                # fallback pt formatul vechi cu *_pts
                if "k_sl" not in params and "stop_loss_pts" in params:
                    params["k_sl"] = float(params["stop_loss_pts"])
                if "k_trg" not in params and "trailing_trigger_pts" in params:
                    params["k_trg"] = float(params["trailing_trigger_pts"])
                if "k_dist" not in params and "trailing_dist_pts" in params:
                    params["k_dist"] = float(params["trailing_dist_pts"])

                _LIVE_PARAMS_CACHE = params
                log_info(f"[LIVE PARAMS] Loaded best_params from {path}: {params}")
                return _LIVE_PARAMS_CACHE
        except Exception as e:
            log_error(f"[LIVE PARAMS] Failed to load {path}: {e}")

    log_error("[LIVE PARAMS] Could not find best_params.json; using empty params.")
    _LIVE_PARAMS_CACHE = {}
    return _LIVE_PARAMS_CACHE

# ----------------------------------------------------------------------
# Funcții MT5 de bază
# ----------------------------------------------------------------------

def initialize_mt5():
    for attempt in range(3):
        if mt5.initialize():
            account_info = mt5.account_info()
            if account_info:
                log_info(f"Logged in as {account_info.login} (Balance: {account_info.balance})")
                return True
            else:
                log_error("Failed to retrieve account info.")
                mt5.shutdown()
        log_error("MT5 initialization failed. Retrying...")
        tmod.sleep(5)
    return False

def shutdown_mt5():
    mt5.shutdown()
    log_info("MT5 connection closed")

def fetch_historical_data(symbol, timeframe, Bars):
    if not mt5.symbol_select(symbol, True):
        log_error(f"Symbol {symbol} not available in MT5.")
        return pd.DataFrame()
    rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, Bars)
    if rates is None or len(rates) == 0:
        log_error(f"Failed to fetch data for {symbol}")
        return pd.DataFrame()
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s")
    return df

def get_open_chart():
    try:
        return MANUAL_SYMBOL, MANUAL_TIMEFRAME
    except Exception as e:
        log_error(f"Failed to retrieve open chart: {e}")
    return None, None

# ----------------------------------------------------------------------
# Stop adaptiv (vechi) – lăsat pt compatibilitate
# ----------------------------------------------------------------------

def calculate_adaptive_stop(symbol, lot, risk_amount, timeframe, Bars,
                            use_atr=True, atr_period=14, atr_factor=1.0):
    """
    Versiune veche: stop = max(risk-based, ATR-based).
    O păstrăm în caz că o mai folosești, dar execute_trade folosește
    logica 1:1 cu backtesterul.
    """
    info = mt5.symbol_info(symbol)
    if info is None:
        raise RuntimeError(f"Symbol info not found: {symbol}")

    point         = info.point
    contract_size = info.trade_contract_size
    min_stop_pts  = info.trade_stops_level

    value_per_point = lot * contract_size
    pts_from_risk   = risk_amount / value_per_point

    pts_from_atr = 0.0
    if use_atr:
        import pandas_ta as ta
        df = fetch_historical_data(symbol, timeframe, Bars)
        if df.empty:
            raise RuntimeError("No data for ATR calculation")
        df["atr"] = ta.atr(df["high"], df["low"], df["close"], length=atr_period)
        latest_atr = df["atr"].iloc[-1]
        pts_from_atr = (latest_atr / point) * atr_factor

    raw_sl_pts    = max(pts_from_risk, pts_from_atr)
    sl_pts        = max(raw_sl_pts, min_stop_pts) + 1
    stop_distance = sl_pts * point
    return stop_distance

# ----------------------------------------------------------------------
# EXECUTE_TRADE – logică 1:1 cu backtesterul (ATR + risk sizing)
# ----------------------------------------------------------------------

def execute_trade(symbol, action, price, timeframe, Bars, stop_loss=None, tp=None):
    """
    Live trading cu aceleași reguli ca backtesterul:

      SL distance:
        sl_dist = atr_pts * point * k_sl * (equity / BASE_BALANCE)^RISK_EXPONENT

      Trailing (salvat în comment):
        trig_dist  = atr_pts * point * k_trg * risk_scale
        trail_dist = atr_pts * point * k_dist * risk_scale

      Lot sizing:
        dacă ENABLE_RISK_SIZING:
            risk_cash = equity * RISK_PER_TRADE
            lots = risk_cash / (sl_dist * contract_size)
        altfel:
            lots = LOT_SIZE
    """

    if not mt5.initialize():
        log_error("MT5 is not initialized.")
        return False

    account_info = mt5.account_info()
    if account_info is None or not account_info.trade_allowed:
        log_error("Trading not allowed or not logged in.")
        return False

    if not mt5.symbol_select(symbol, True):
        log_error(f"Symbol {symbol} not available in MT5.")
        return False

    # Închide poziția opusă, evită dublurile pe aceeași direcție
    positions = mt5.positions_get(symbol=symbol) or []
    for pos in positions:
        if (pos.type == mt5.ORDER_TYPE_BUY and action == "buy") or \
           (pos.type == mt5.ORDER_TYPE_SELL and action == "sell"):
            log_info(f"Skipped {action.upper()}: already open on {symbol}")
            return False
        if pos.type == mt5.ORDER_TYPE_BUY and action == "sell":
            log_info(f"Closing opposite BUY for {symbol}")
            close_position(pos)
        elif pos.type == mt5.ORDER_TYPE_SELL and action == "buy":
            log_info(f"Closing opposite SELL for {symbol}")
            close_position(pos)

    symbol_info = mt5.symbol_info(symbol)
    if symbol_info is None:
        log_error(f"Failed to get symbol info for {symbol}")
        return False

    point         = symbol_info.point
    contract_size = symbol_info.trade_contract_size
    equity        = float(account_info.equity)

    # ----------------- Parametrii de ATR & k_* din backtester ------------- #

    live_params = _load_live_params()

    atr_period = int(
        live_params.get("atr_period",
                        live_params.get("supertrend_period", 14))
    )
    k_sl   = float(live_params.get("k_sl",   1.5))
    k_trg  = float(live_params.get("k_trg",  3.0))
    k_dist = float(live_params.get("k_dist", 1.5))

    df = fetch_historical_data(symbol, timeframe, Bars)
    if df.empty:
        log_error("No data for ATR calculation in live trading.")
        return False

    atr_price_series = compute_atr_series(df, atr_period)
    if atr_price_series.isna().all():
        log_error("ATR series is NaN in live trading.")
        return False

    atr_price_latest = float(atr_price_series.iloc[-1])
    atr_pts = atr_price_latest / point if point > 0 else 0.0

    if atr_pts <= 0:
        log_error("[LIVE RISK] ATR points <= 0; aborting trade.")
        return False

    # risk_scale = (equity / BASE_BALANCE)^RISK_EXPONENT
    if BASE_BALANCE_SAFE > 0:
        risk_scale = (equity / BASE_BALANCE_SAFE) ** RISK_EXPONENT_SAFE
    else:
        risk_scale = 1.0

    # distanțe în preț (ca în backtester)
    sl_dist    = atr_pts * point * k_sl   * risk_scale
    trig_dist  = atr_pts * point * k_trg  * risk_scale
    trail_dist = atr_pts * point * k_dist * risk_scale

    if sl_dist <= 0:
        log_error("[LIVE RISK] Computed SL distance <= 0; aborting trade.")
        return False

    # ----------------- Lot sizing identic cu backtester ------------------- #

    lots = float(LOT_SIZE)

    if ENABLE_RISK_SIZING_SAFE:
        risk_cash = equity * RISK_PER_TRADE_SAFE
        denom = max(1e-9, sl_dist * contract_size)
        raw_lots = risk_cash / denom if denom > 0 else lots
        lots = quantize_lot(symbol_info, raw_lots)
        if lots <= 0.0:
            lots = float(LOT_SIZE)
        log_info(
            f"[LIVE RISK] equity={equity:.2f}, risk_cash={risk_cash:.2f}, "
            f"sl_dist={sl_dist:.5f}, k_sl={k_sl}, risk_scale={risk_scale:.4f}, "
            f"lots={lots:.2f}"
        )
    else:
        log_info(f"[LIVE RISK] ENABLE_RISK_SIZING=False, using fixed LOT_SIZE={lots}")

    # ----------------- Stop Loss (dacă nu e dat explicit) ----------------- #

    if stop_loss is None:
        if action == "buy":
            stop_loss = price - sl_dist
        else:
            stop_loss = price + sl_dist

    stop_loss = round(stop_loss, symbol_info.digits)

    # trailing în pips (pentru comment)
    trig_pips  = trig_dist / point
    trail_pips = trail_dist / point

    # TP îl lăsăm opțional (backtester folosește trailing, nu TP fix)
    if tp is None:
        tp = 0.0

    comment = f"AutoTrade|trig={trig_pips:.1f}|dist={trail_pips:.1f}"

    # ----------------- Construire request MT5 ----------------------------- #

    request = {
        "action":   mt5.TRADE_ACTION_DEAL,
        "symbol":   symbol,
        "volume":   lots,
        "type":     mt5.ORDER_TYPE_BUY if action == "buy" else mt5.ORDER_TYPE_SELL,
        "price":    price,
        "sl":       stop_loss,
        "tp":       tp,
        "deviation":50,
        "magic":    123456,
        "comment":  comment,
        "type_time":mt5.ORDER_TIME_GTC,
    }

    # Testăm mai multe tipuri de filling
    fill_modes = [
        mt5.ORDER_FILLING_IOC,
        mt5.ORDER_FILLING_FOK,
        mt5.ORDER_FILLING_RETURN
    ]
    for mode in fill_modes:
        request["type_filling"] = mode
        log_info(f"Trying fill mode {mode} for {action.upper()} {symbol}")
        result = mt5.order_send(request)
        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            log_info(f"Trade executed successfully with mode={mode}, lots={lots}")
            log_info(
                f"[LIVE TRAIL] trig={trig_pips:.1f} pips, "
                f"dist={trail_pips:.1f} pips"
            )
            return True
        elif result:
            log_error(f"Mode {mode} failed: {result.retcode} - {result.comment}")

    # Fallback: mai încercăm de câteva ori
    log_info("Retrying final attempts with default filling mode")
    for attempt in range(3):
        result = mt5.order_send(request)
        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            log_info(f"Trade executed on attempt {attempt+1}, lots={lots}")
            log_info(
                f"[LIVE TRAIL] trig={trig_pips:.1f} pips, "
                f"dist={trail_pips:.1f} pips"
            )
            return True
        elif result:
            log_error(f"Attempt {attempt+1} failed: {result.retcode} - {result.comment}")
        tmod.sleep(2)

    return False

# ----------------------------------------------------------------------
# Close & trailing – trailing citește distanțele din comment (1:1)
# ----------------------------------------------------------------------

def close_position(position):
    close_type = mt5.ORDER_TYPE_SELL if position.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
    tick = mt5.symbol_info_tick(position.symbol)
    if tick is None:
        log_error(f"Failed to get tick for {position.symbol}")
        return False
    price = tick.bid if close_type == mt5.ORDER_TYPE_SELL else tick.ask

    request = {
        "action":     mt5.TRADE_ACTION_DEAL,
        "symbol":     position.symbol,
        "volume":     position.volume,
        "type":       close_type,
        "position":   position.ticket,
        "price":      price,
        "deviation":  20,
        "magic":      123456,
        "comment":    "AutoClose",
        "type_time":  mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }
    result = mt5.order_send(request)
    if result and result.retcode == mt5.TRADE_RETCODE_DONE:
        log_info(f"Closed trade {position.ticket} on {position.symbol} at {price}")
        return True
    else:
        log_error(f"Failed to close position {position.ticket}: {getattr(result, 'retcode', 'N/A')}")
        return False


def adjust_trailing_stop(trailing_trigger_pips=None, trailing_dist_pips=None):
    """
    Mută SL după ce profitul >= trig, folosind trig/dist salvate în comment:

      comment = "AutoTrade|trig=XX.X|dist=YY.Y"

    Dacă nu le găsește în comment, folosește valorile primite ca argumente.
    """
    if not TRAILING_STOP_ENABLED:
        return

    positions = mt5.positions_get() or []
    if not positions:
        return

    for pos in positions:
        symbol = pos.symbol
        info   = mt5.symbol_info(symbol)
        tick   = mt5.symbol_info_tick(symbol)
        if info is None or tick is None:
            continue

        # încearcă să citească trig/dist din comment
        trig_pips = None
        dist_pips = None
        if isinstance(pos.comment, str) and "AutoTrade" in pos.comment:
            parts = pos.comment.split("|")
            for p in parts:
                if p.startswith("trig="):
                    try:
                        trig_pips = float(p.split("=", 1)[1])
                    except ValueError:
                        pass
                if p.startswith("dist="):
                    try:
                        dist_pips = float(p.split("=", 1)[1])
                    except ValueError:
                        pass

        # fallback la parametrii funcției dacă nu găsim în comment
        if trig_pips is None:
            trig_pips = trailing_trigger_pips
        if dist_pips is None:
            dist_pips = trailing_dist_pips

        if trig_pips is None or dist_pips is None:
            continue  # nu avem info suficient

        current_price = tick.ask if pos.type == mt5.ORDER_TYPE_BUY else tick.bid
        entry_price   = pos.price_open
        profit_pips   = (current_price - entry_price) / info.point
        profit_pips   = profit_pips if profit_pips >= 0 else -profit_pips

        if profit_pips >= trig_pips:
            if pos.type == mt5.ORDER_TYPE_BUY:
                new_sl = current_price - (dist_pips * info.point)
                move_ok = (pos.sl == 0 or new_sl > pos.sl)
            else:
                new_sl = current_price + (dist_pips * info.point)
                move_ok = (pos.sl == 0 or new_sl < pos.sl)

            if move_ok:
                req = {
                    "action":   mt5.TRADE_ACTION_SLTP,
                    "position": pos.ticket,
                    "sl":       round(new_sl, info.digits),
                    "tp":       pos.tp
                }
                res = mt5.order_send(req)
                if res and res.retcode == mt5.TRADE_RETCODE_DONE:
                    log_info(f"Trailing SL updated for {symbol} to {new_sl}")
