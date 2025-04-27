import pandas as pd
import pandas_ta as ta
from utils import log_info, log_error

def calculate_indicators(df, params):
    if df.empty:
        log_error("Empty DataFrame.")
        return df

    if not pd.api.types.is_datetime64_any_dtype(df['time']):
        df['time'] = pd.to_datetime(df['time'])

    df.set_index('time', inplace=True)

    df.ta.supertrend(length=params["supertrend_period"], multiplier=params["supertrend_multiplier"], append=True)
    supertrend_col = f"SUPERTd_{params['supertrend_period']}_{params['supertrend_multiplier']:.1f}"
    supertrend_lower_col = f"SUPERTl_{params['supertrend_period']}_{params['supertrend_multiplier']:.1f}"

    if supertrend_col not in df.columns:
        log_error(f"Missing {supertrend_col} column.")
        return df

    df["supertrend_signal"] = df[supertrend_col].map({1: "buy", -1: "sell"}).fillna("hold")

    if supertrend_lower_col in df.columns:
        df[supertrend_lower_col] = df[supertrend_lower_col].ffill()

    adx = ta.adx(df['high'], df['low'], df['close'], length=params["adx_period"])
    df["adx"] = adx[f"ADX_{params['adx_period']}"].ffill()

    df["rsi"] = ta.rsi(df['close'], length=params["rsi_period"]).ffill()

    log_info(df.tail(5))
    return df
