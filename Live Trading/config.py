import MetaTrader5 as mt5
from datetime import time
import os, json

# ===== MT5 credentials =====
MT5_ACCOUNT  = int(os.getenv("MT5_ACCOUNT", "52333432"))
MT5_PASSWORD = os.getenv("MT5_PASSWORD", "0hi!TwIOaL&BYd")
MT5_SERVER   = os.getenv("MT5_SERVER",   "ICMarketsEU-Demo")
MT5_TERMINAL_PATH = os.getenv("MT5_TERMINAL_PATH", r"C:\Program Files\MetaTrader 5\terminal64.exe")

# Trading Settings
START_BALANCE= 100000
Bars = 10000
TRAILING_STOP_ENABLED = True
TRADE_FREQUENCY_SECONDS = 30

# Symbol & Timeframe Settings
USE_MANUAL_SYMBOL = False
MANUAL_SYMBOL = "EURUSD"
MANUAL_TIMEFRAME = mt5.TIMEFRAME_M5
MANUAL_PARAMS = {
    "supertrend_period": 10,
    "supertrend_multiplier": 3,
    "adx_period": 14,
    "adx_threshold": 25,
    "rsi_period": 14,
    "rsi_oversold": 30,
    "rsi_overbought": 70
}

# ===== Costs / sizing =====
ENABLE_RISK_SIZING=True
RISK_PER_TRADE=float(os.getenv("RISK_PER_TRADE", "0.2"))
RISK_EXPONENT=float(os.getenv("RISK_EXPONENT", "3.0"))
BASE_BALANCE=float(os.getenv("BASE_BALANCE", "10000"))
LOT_SIZE           = float(os.getenv("LOT_SIZE", "0.9"))

# Prop firm logic
FUNDED_MODE = True
DAILY_MAX_LOSS_PERCENT = 4.5  # If needed in future

# Closing Hour
CLOSE_HOUR = time(15,19 )

# ─────────────────────────────────────────────────────────────────────────────
# NEWS FILTER (same rules as backtester)
# ─────────────────────────────────────────────────────────────────────────────
def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None: return default
    return v.strip().lower() in ("1","true","yes","y","on")

USE_NEWS_FILTER = _env_bool("USE_NEWS_FILTER", False)

# ===== Economic calendar provider (RapidAPI: Investing.com Ultimate API) =====
RAPIDAPI_BASE     = os.getenv("RAPIDAPI_BASE", "https://investing-com-ultimate-api.p.rapidapi.com")
RAPIDAPI_ENDPOINT = os.getenv("RAPIDAPI_ENDPOINT", "/news/economic-calendar")
RAPIDAPI_HOST     = os.getenv("RAPIDAPI_HOST", "investing-com-ultimate-api.p.rapidapi.com")
RAPIDAPI_KEY      = os.getenv("RAPIDAPI_KEY", "6e0941c375mshb2b263483eab43bp151923jsn918726b523e5")  # set via environment

# Vendor usually returns dd/mm/YYYY
RAPIDAPI_DATE_FMT = os.getenv("RAPIDAPI_DATE_FMT", "%d/%m/%Y")

# Countries & importances to fetch (we fetch all to catch holidays, then filter locally)
RAPIDAPI_COUNTRIES = json.loads(os.getenv(
    "RAPIDAPI_COUNTRIES_JSON",
    '["united states","united kingdom","euro zone","germany"]'
))
RAPIDAPI_IMPORTANCES = json.loads(os.getenv(
    "RAPIDAPI_IMPORTANCES_JSON",
    '["high"]'
))

# Full-day keywords (FOMC/CPI/GDP/BoE + holidays)
NEWS_FULLDAY_KEYWORDS = json.loads(os.getenv(
    "NEWS_FULLDAY_KEYWORDS_JSON",
    '["FOMC","Fed Interest Rate Decision","FOMC Press Conference","Federal Reserve",'
    ' "CPI","HICP","Inflation Rate","GDP","Gross Domestic Product",'
    ' "BoE Interest Rate Decision","Monetary Policy Report","MPC Minutes","BoE Press Conference",'
    ' "Bank Holiday","Public Holiday","Market Holiday","National Holiday"]'
))

# Pause: only for high-importance (3-bull) events
NEWS_PAUSE_IMPORTANCES = json.loads(os.getenv(
    "NEWS_PAUSE_IMPORTANCES_JSON", '["high"]'
))
NEWS_PAUSE_MIN_BEFORE = int(os.getenv("NEWS_PAUSE_PRE_MIN", "30"))
NEWS_PAUSE_MIN_AFTER  = int(os.getenv("NEWS_PAUSE_POST_MIN", "30"))

# Optional: full-day by impact (we keep empty; full day is keyword-driven)
NEWS_FULL_DAY_IMPORTANCES = json.loads(os.getenv("NEWS_FULL_DAY_IMPORTANCES_JSON", "[]"))
# Optional extra pause keywords
NEWS_KEYWORDS_PAUSE = json.loads(os.getenv("NEWS_KEYWORDS_PAUSE_JSON", "[]"))

# Cache & retries (same behavior as backtester)
NEWS_MAX_RETRIES_429    = int(os.getenv("NEWS_MAX_RETRIES_429", "4"))
NEWS_RETRY_BASE_DELAY_S = float(os.getenv("NEWS_RETRY_BASE_DELAY_S", "1.5"))
NEWS_CACHE_DIR          = os.getenv("NEWS_CACHE_DIR", "cache/news")
NEWS_CACHE_TTL_DAYS     = int(os.getenv("NEWS_CACHE_TTL_DAYS", "30"))
NEWS_DEBUG              = os.getenv("NEWS_DEBUG", "1")

# Live: how many days ahead to fetch
NEWS_LOOKAHEAD_DAYS = int(os.getenv("NEWS_LOOKAHEAD_DAYS", "0"))

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

    env.setdefault("NEWS_MAX_RETRIES_429",   str(NEWS_MAX_RETRIES_429))
    env.setdefault("NEWS_RETRY_BASE_DELAY_S", str(NEWS_RETRY_BASE_DELAY_S))
    env.setdefault("NEWS_CACHE_DIR", NEWS_CACHE_DIR)
    env.setdefault("NEWS_CACHE_TTL_DAYS", str(NEWS_CACHE_TTL_DAYS))
    env.setdefault("NEWS_DEBUG", NEWS_DEBUG)

# export on import
apply_news_env_from_config()

