import MetaTrader5 as mt5
import json
import pandas as pd
import time as tmod
from config import *
from utils import log_info, log_error

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
    df['time'] = pd.to_datetime(df['time'], unit='s')
    return df

def get_open_chart():
    try:
        return MANUAL_SYMBOL, MANUAL_TIMEFRAME
    except Exception as e:
        log_error(f"Failed to retrieve open chart: {e}")
    return None, None

def calculate_adaptive_stop(symbol, lot, risk_amount, timeframe, Bars,
                            use_atr=True, atr_period=14, atr_factor=1.0):
    """
    Compute stop‐loss distance (in price units) as max(1% risk, ATR‐based)
    Ensures it’s ≥ MT5’s minimum stop distance.
    """
    info = mt5.symbol_info(symbol)
    if info is None:
        raise RuntimeError(f"Symbol info not found: {symbol}")

    point         = info.point
    contract_size = info.trade_contract_size
    min_stop_pts  = info.trade_stops_level

    # 1) risk‐based points
    value_per_point = lot * contract_size
    pts_from_risk   = risk_amount / value_per_point

    # 2) ATR‐based points
    pts_from_atr = 0.0
    if use_atr:
        import pandas_ta as ta
        df = fetch_historical_data(symbol, timeframe, Bars)
        if df.empty:
            raise RuntimeError("No data for ATR calculation")
        df['atr'] = ta.atr(df["high"], df["low"], df["close"], length=atr_period)
        latest_atr = df['atr'].iloc[-1]
        pts_from_atr = (latest_atr / point) * atr_factor

    # 3) choose the larger, enforce minimum, add buffer, convert to price
    raw_sl_pts    = max(pts_from_risk, pts_from_atr)
    sl_pts        = max(raw_sl_pts, min_stop_pts) + 1
    stop_distance = sl_pts * point
    return stop_distance

def execute_trade(symbol, action, price, timeframe, Bars, stop_loss=None, tp=None):
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

    # Prevent same‐direction duplicates, close opposite if present
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

    # Determine stop_loss if not provided
    if stop_loss is None:
        equity      = account_info.equity
        risk_amount = equity * 0.01  # 1% risk
        sl_dist = calculate_adaptive_stop(
            symbol, lot=LOT_SIZE, risk_amount=risk_amount,
            timeframe=timeframe, Bars=Bars,
            use_atr=True, atr_period=14, atr_factor=1.0
        )
        stop_loss = price - sl_dist if action == "buy" else price + sl_dist

    # Build request
    request = {
        "action":   mt5.TRADE_ACTION_DEAL,
        "symbol":   symbol,
        "volume":   LOT_SIZE,
        "type":     mt5.ORDER_TYPE_BUY if action == "buy" else mt5.ORDER_TYPE_SELL,
        "price":    price,
        "sl":       round(stop_loss, symbol_info.digits),
        "tp":       tp or 0.0,
        "deviation":50,
        "magic":    123456,
        "comment":  "AutoTrade",
        "type_time":mt5.ORDER_TIME_GTC,
    }

    # Try filling modes in order
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
            log_info(f"Trade executed successfully with mode={mode}")
            return True
        elif result:
            log_error(f"Mode {mode} failed: {result.retcode} - {result.comment}")

    # Fallback: retry with default
    log_info("Retrying final attempts with default filling mode")
    for attempt in range(3):
        result = mt5.order_send(request)
        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            log_info(f"Trade executed on attempt {attempt+1}")
            return True
        elif result:
            log_error(f"Attempt {attempt+1} failed: {result.retcode} - {result.comment}")
        tmod.sleep(2)

    return False

def close_position(position):
    close_type = mt5.ORDER_TYPE_SELL if position.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
    price      = (mt5.symbol_info_tick(position.symbol).bid
                  if close_type == mt5.ORDER_TYPE_SELL
                  else mt5.symbol_info_tick(position.symbol).ask)
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

def adjust_trailing_stop(trailing_trigger_pips, trailing_dist_pips):
    """
    Move the stop-loss once profit >= trailing_trigger_pips,
    keeping it trailing by trailing_dist_pips.
    """
    if not TRAILING_STOP_ENABLED:
        return

    positions = mt5.positions_get() or []
    if not positions:
        log_info("No open positions.")
        return

    for pos in positions:
        symbol = pos.symbol
        info   = mt5.symbol_info(symbol)
        tick   = mt5.symbol_info_tick(symbol)
        if info is None or tick is None:
            log_error(f"Failed to get data for {symbol}")
            continue

        current_price = tick.ask if pos.type == mt5.ORDER_TYPE_BUY else tick.bid
        entry_price   = pos.price_open
        profit_pips   = abs((current_price - entry_price) / info.point)

        if profit_pips >= trailing_trigger_pips:
            if pos.type == mt5.ORDER_TYPE_BUY:
                new_sl = current_price - (trailing_dist_pips * info.point)
                move_ok = (pos.sl == 0 or new_sl > pos.sl)
            else:
                new_sl = current_price + (trailing_dist_pips * info.point)
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
                else:
                    log_error(f"Failed to update trailing SL for {symbol}: {getattr(res, 'retcode', 'N/A')}")
