# config.py — backtester config + Investing.com news rules
import os, json
from datetime import time
import MetaTrader5 as mt5

# ===== Feature flags =====
USE_ECONOMIC_CALENDAR = True
USE_NEWS_FILTER = True

# ===== MT5 credentials =====
MT5_ACCOUNT  = int(os.getenv("MT5_ACCOUNT", "7120278"))
MT5_PASSWORD = os.getenv("MT5_PASSWORD", "Asditon99@!")
MT5_SERVER   = os.getenv("MT5_SERVER",   "FirstPrudentialMarkets-Demo")
MT5_TERMINAL_PATH = os.getenv("MT5_TERMINAL_PATH", r"C:\Program Files\MetaTrader 5\terminal64.exe")

# NEW: full fixed setup (symbol + timeframe + params)
USE_FIXED_SETUP = False
USE_FIXED_PARAMS = False

# Provide your fixed setup here (or via FIXED_SETUP_JSON env)
_DEFAULT_FIXED_SETUP = {
    "symbol": "EURUSD",
    "timeframe": 5,
    "params": {
        "supertrend_period": 5,
        "supertrend_multiplier": 3,
        "adx_period": 10,
        "adx_threshold": 20,
        "rsi_period": 15,
        "rsi_oversold": 25,
        "rsi_overbought": 60,
        "stop_loss_pts": 45,
        "trailing_trigger_pts": 99,
        "trailing_dist_pts": 88
    }
}
try:
    FIXED_SETUP = json.loads(os.getenv("FIXED_SETUP_JSON", "")) or _DEFAULT_FIXED_SETUP
except Exception:
    FIXED_SETUP = _DEFAULT_FIXED_SETUP

# Provide your fixed params (used only if USE_FIXED_SETUP=False and USE_FIXED_PARAMS=True)
_DEFAULT_FIXED_PARAMS = _DEFAULT_FIXED_SETUP["params"]
try:
    FIXED_PARAMS = json.loads(os.getenv("FIXED_PARAMS_JSON", "")) or _DEFAULT_FIXED_PARAMS
except Exception:
    FIXED_PARAMS = _DEFAULT_FIXED_PARAMS

# ===== Universe / Dates =====
TIMEFRAME_LIST = [mt5.TIMEFRAME_M5, mt5.TIMEFRAME_M15,mt5.TIMEFRAME_M1]
SYMBOL_LIST    = ["EURUSD","GBPUSD","US30","UK100","GER40"]

BACKTEST_START_DATE = os.getenv("BACKTEST_START_DATE", "2025-09-01")
BACKTEST_END_DATE   = os.getenv("BACKTEST_END_DATE",   "2025-09-26")

# ===== Trading session & weekends =====
ALLOWED_SESSIONS = [(time(8, 0), time(18, 0))]  # local
WEEKEND_DAYS = [5, 6]  # Sat, Sun

# ===== Costs / sizing =====
SPREAD_PIPS        = float(os.getenv("SPREAD_PIPS", "0.0"))
COMMISSION_PER_LOT = float(os.getenv("COMMISSION_PER_LOT", "0.0"))
SLIPPAGE_PIPS      = float(os.getenv("SLIPPAGE_PIPS", "0.0"))
LOT_SIZE           = float(os.getenv("LOT_SIZE", "0.9"))

# ===== Param space (your original) =====
PARAM_GRID = {
    "supertrend_period":     [7, 10, 14],
    "supertrend_multiplier": [2.0, 3.0],
    "atr_period":            [14],
    "rsi_period":            [14],
    "rsi_overbought":        [70],
    "rsi_oversold":          [30],
    "adx_period":            [14],
    "adx_threshold":         [20, 25],
}

# ===== Funded / risk constants (needed by funded_risk.py) =====
def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None: return default
    return v.strip().lower() in ("1","true","yes","y","on")

START_BALANCE = float(os.getenv("START_BALANCE", "100000"))
DAILY_MAX_LOSS_PERCENT = float(os.getenv("DAILY_MAX_LOSS_PERCENT", "4.5"))
MAX_TOTAL_LOSS_PERCENT = float(os.getenv("MAX_TOTAL_LOSS_PERCENT", "10.0"))
STAGE2_STEP_RISK_PCT = float(os.getenv("STAGE2_STEP_RISK_PCT", "0.10"))
STAGE2_MAX_RISK_PCT  = float(os.getenv("STAGE2_MAX_RISK_PCT",  "1.00"))
FUNDED_MODE = _env_bool("FUNDED_MODE", True)

# ===== Economic calendar provider (RapidAPI: Investing.com Ultimate API) =====
RAPIDAPI_BASE     = os.getenv("RAPIDAPI_BASE", "https://investing-com-ultimate-api.p.rapidapi.com")
RAPIDAPI_ENDPOINT = os.getenv("RAPIDAPI_ENDPOINT", "/news/economic-calendar")
RAPIDAPI_HOST     = os.getenv("RAPIDAPI_HOST", "investing-com-ultimate-api.p.rapidapi.com")
RAPIDAPI_KEY      = os.getenv("RAPIDAPI_KEY", "6e0941c375mshb2b263483eab43bp151923jsn918726b523e5")  # set via environment

# Vendor expects dd/mm/YYYY
RAPIDAPI_DATE_FMT = os.getenv("RAPIDAPI_DATE_FMT", "%d/%m/%Y")

# Countries to fetch (lowercase works best)
RAPIDAPI_COUNTRIES = json.loads(os.getenv(
    "RAPIDAPI_COUNTRIES_JSON",
    '["united states","united kingdom","euro zone","germany"]'
))

# IMPORTANT: fetch *all* importances so we don't miss bank/public holidays,
# then we decide locally what to full-day vs pause.
RAPIDAPI_IMPORTANCES = json.loads(os.getenv(
    "RAPIDAPI_IMPORTANCES_JSON",
    '["low","medium","high"]'
))

# ===== News behavior you requested =====
# Full-day skip for these keywords (case-insensitive substring match):
NEWS_FULLDAY_KEYWORDS = json.loads(os.getenv(
    "NEWS_FULLDAY_KEYWORDS_JSON",
    # FOMC + Fed, CPI/HICP, GDP, BoE policy day, and any holiday terms
    '["FOMC","Fed Interest Rate Decision","FOMC Press Conference","Federal Reserve",'
    ' "CPI","HICP","Inflation Rate","GDP","Gross Domestic Product",'
    ' "BoE Interest Rate Decision","Monetary Policy Report","MPC Minutes","BoE Press Conference",'
    ' "Bank Holiday","Public Holiday","Market Holiday","National Holiday"]'
))

# Only *high*-importance (3-bull) events cause pauses; others ignored.
NEWS_PAUSE_IMPORTANCES = json.loads(os.getenv(
    "NEWS_PAUSE_IMPORTANCES_JSON",
    '["high"]'
))

# Pause window around pausable events (minutes)
NEWS_PAUSE_MIN_BEFORE = int(os.getenv("NEWS_PAUSE_PRE_MIN", "30"))
NEWS_PAUSE_MIN_AFTER  = int(os.getenv("NEWS_PAUSE_POST_MIN", "30"))

# We do NOT full-day by importance; only by keywords above.
NEWS_FULL_DAY_IMPORTANCES = json.loads(os.getenv(
    "NEWS_FULL_DAY_IMPORTANCES_JSON", "[]"
))

# Optional extra pause keywords (kept empty by default)
NEWS_KEYWORDS_PAUSE = json.loads(os.getenv("NEWS_KEYWORDS_PAUSE_JSON", "[]"))

# Timezone hint for event times (US events are often ET)
NEWS_TIMEZONE = os.getenv("NEWS_TIMEZONE", "America/New_York")

# Retries & cache
NEWS_MAX_RETRIES_429    = int(os.getenv("NEWS_MAX_RETRIES_429", "4"))
NEWS_RETRY_BASE_DELAY_S = float(os.getenv("NEWS_RETRY_BASE_DELAY_S", "1.5"))
NEWS_CACHE_DIR          = os.getenv("NEWS_CACHE_DIR", "cache/news")
NEWS_CACHE_TTL_DAYS     = int(os.getenv("NEWS_CACHE_TTL_DAYS", "30"))
NEWS_DEBUG              = os.getenv("NEWS_DEBUG", "1")

def apply_news_env_from_config():
    env = os.environ
    env.setdefault("RAPIDAPI_BASE", RAPIDAPI_BASE)
    env.setdefault("RAPIDAPI_ENDPOINT", RAPIDAPI_ENDPOINT)
    env.setdefault("RAPIDAPI_HOST", RAPIDAPI_HOST)
    env.setdefault("RAPIDAPI_KEY", RAPIDAPI_KEY)
    env.setdefault("RAPIDAPI_DATE_FMT", RAPIDAPI_DATE_FMT)

    env.setdefault("RAPIDAPI_COUNTRIES_JSON", json.dumps(RAPIDAPI_COUNTRIES))
    env.setdefault("RAPIDAPI_IMPORTANCES_JSON", json.dumps(RAPIDAPI_IMPORTANCES))

    env.setdefault("NEWS_FULLDAY_KEYWORDS_JSON", json.dumps(NEWS_FULLDAY_KEYWORDS))
    env.setdefault("NEWS_PAUSE_IMPORTANCES_JSON", json.dumps(NEWS_PAUSE_IMPORTANCES))
    env.setdefault("NEWS_PAUSE_PRE_MIN",  str(NEWS_PAUSE_MIN_BEFORE))
    env.setdefault("NEWS_PAUSE_POST_MIN", str(NEWS_PAUSE_MIN_AFTER))
    env.setdefault("NEWS_FULL_DAY_IMPORTANCES_JSON", json.dumps(NEWS_FULL_DAY_IMPORTANCES))
    env.setdefault("NEWS_KEYWORDS_PAUSE_JSON", json.dumps(NEWS_KEYWORDS_PAUSE))
    env.setdefault("NEWS_TIMEZONE", NEWS_TIMEZONE)

    env.setdefault("NEWS_MAX_RETRIES_429",   str(NEWS_MAX_RETRIES_429))
    env.setdefault("NEWS_RETRY_BASE_DELAY_S", str(NEWS_RETRY_BASE_DELAY_S))
    env.setdefault("NEWS_CACHE_DIR", NEWS_CACHE_DIR)
    env.setdefault("NEWS_CACHE_TTL_DAYS", str(NEWS_CACHE_TTL_DAYS))
    env.setdefault("NEWS_DEBUG", NEWS_DEBUG)

# Export on import
apply_news_env_from_config()
