import MetaTrader5 as mt5
import json
import pandas as pd
import time
from config import (
    LOT_SIZE, TRAILING_STOP_TRIGGER_PIPS, TRAILING_STOP_ENABLED,
    TRAILING_STOP_DISTANCE_PIPS, MANUAL_SYMBOL, MANUAL_TIMEFRAME
)
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
        time.sleep(5)
    return False

def shutdown_mt5():
    mt5.shutdown()
    log_info("MT5 connection closed")

def fetch_historical_data(symbol, timeframe, bars=10000):
    if not mt5.symbol_select(symbol, True):
        log_error(f"Symbol {symbol} not available in MT5.")
        return pd.DataFrame()
    rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, bars)
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

def execute_trade(symbol, action, price, stop_loss=None, tp=None):
    if not mt5.initialize():
        log_error("MT5 is not initialized.")
        return False

    account_info = mt5.account_info()
    if account_info is None:
        log_error("MT5 is not logged in. Check your credentials.")
        return False

    if not account_info.trade_allowed:
        log_error("Trading not allowed on this account. Check broker settings.")
        return False

    if not mt5.symbol_select(symbol, True):
        log_error(f"Symbol {symbol} not available in MT5.")
        return False

    positions = mt5.positions_get(symbol=symbol)

    # Prevent opening a new trade in the same direction
    for position in positions:
        if position.type == mt5.ORDER_TYPE_BUY and action == "buy":
            log_info(f"Skipped BUY: already open BUY on {symbol}")
            return False
        if position.type == mt5.ORDER_TYPE_SELL and action == "sell":
            log_info(f"Skipped SELL: already open SELL on {symbol}")
            return False

        # Close opposite direction trade
        if position.type == mt5.ORDER_TYPE_BUY and action == "sell":
            log_info(f"Closing opposite BUY trade for {symbol}")
            close_position(position)
        elif position.type == mt5.ORDER_TYPE_SELL and action == "buy":
            log_info(f"Closing opposite SELL trade for {symbol}")
            close_position(position)

    symbol_info = mt5.symbol_info(symbol)
    if symbol_info is None:
        log_error(f"Failed to get symbol info for {symbol}")
        return False

    if LOT_SIZE < symbol_info.volume_min or LOT_SIZE > symbol_info.volume_max:
        log_error(f"Lot size {LOT_SIZE} is outside allowed range.")
        return False

    price_data = mt5.symbol_info_tick(symbol)
    if price_data is None:
        log_error(f"Failed to get price for {symbol}")
        return False

    ask_price = price_data.ask
    bid_price = price_data.bid
    price = ask_price if action == "buy" else bid_price
    spread = ask_price - bid_price

    if stop_loss is None or stop_loss <= 0:
        if action == "buy":
            stop_loss = price - (50 * symbol_info.point)
        else:
            stop_loss = price + (50 * symbol_info.point)

    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": LOT_SIZE,
        "type": mt5.ORDER_TYPE_BUY if action == "buy" else mt5.ORDER_TYPE_SELL,
        "price": price,
        "sl": round(stop_loss, symbol_info.digits),
        "tp": tp if tp else 0.0,
        "deviation": 50,
        "magic": 123456,
        "comment": "AutoTrade",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": symbol_info.filling_mode
    }

    log_info(f"Sending trade request: {json.dumps(request, indent=4)}")

    for attempt in range(3):
        result = mt5.order_send(request)
        if result is None:
            error = mt5.last_error()
            log_error(f"Trade attempt {attempt + 1} failed. Broker rejection. Error: {error}")
        else:
            log_info(f"MT5 Trade Response (Attempt {attempt + 1}): {result._asdict()}")
            if result.retcode == mt5.TRADE_RETCODE_DONE:
                log_info(f"Trade executed: {action.upper()} {symbol} at {price}")
                return True
            else:
                log_error(f"Trade failed (Attempt {attempt + 1}): Retcode {result.retcode} - {result.comment}")
        time.sleep(2)

    return False

def close_position(position):
    close_type = mt5.ORDER_TYPE_SELL if position.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
    price = mt5.symbol_info_tick(position.symbol).bid if close_type == mt5.ORDER_TYPE_SELL else mt5.symbol_info_tick(position.symbol).ask

    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": position.symbol,
        "volume": position.volume,
        "type": close_type,
        "position": position.ticket,
        "price": price,
        "deviation": 20,
        "magic": 123456,
        "comment": "AutoClose",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }

    result = mt5.order_send(request)
    if result is not None and result.retcode == mt5.TRADE_RETCODE_DONE:
        log_info(f"Closed trade {position.ticket} ({'BUY' if position.type == 0 else 'SELL'}) at {price}")
        return True
    else:
        log_error(f"Failed to close position {position.ticket}. Retcode: {getattr(result, 'retcode', 'N/A')}")
        return False


def adjust_trailing_stop():
    if not TRAILING_STOP_ENABLED:
        return

    positions = mt5.positions_get()
    if not positions:
        log_info("No open positions.")
        return

    for position in positions:
        symbol = position.symbol
        order_type = position.type
        tick_data = mt5.symbol_info_tick(symbol)

        if tick_data is None:
            log_error(f"Failed to get tick data for {symbol}")
            continue

        symbol_info = mt5.symbol_info(symbol)
        if symbol_info is None:
            log_error(f"Failed to get symbol info for {symbol}")
            continue

        current_price = tick_data.ask if order_type == mt5.ORDER_TYPE_BUY else tick_data.bid
        entry_price = position.price_open
        profit_pips = abs((current_price - entry_price) / symbol_info.point)

        if profit_pips >= TRAILING_STOP_TRIGGER_PIPS:
            new_sl = (
                current_price - (TRAILING_STOP_DISTANCE_PIPS * symbol_info.point)
                if order_type == mt5.ORDER_TYPE_BUY else
                current_price + (TRAILING_STOP_DISTANCE_PIPS * symbol_info.point)
            )

            if (order_type == mt5.ORDER_TYPE_BUY and (position.sl == 0 or new_sl > position.sl)) or \
               (order_type == mt5.ORDER_TYPE_SELL and (position.sl == 0 or new_sl < position.sl)):
                modify_request = {
                    "action": mt5.TRADE_ACTION_SLTP,
                    "position": position.ticket,
                    "sl": round(new_sl, symbol_info.digits),
                    "tp": position.tp
                }

                result = mt5.order_send(modify_request)
                if result and result.retcode == mt5.TRADE_RETCODE_DONE:
                    log_info(f"Trailing Stop updated for {symbol} at {new_sl}")
                else:
                    log_error(f"Failed to update trailing stop for {symbol}: {result.retcode}")
