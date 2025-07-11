# news_filters.py — Investing.com Ultimate API with rules:
# - Full-day skip: FOMC/CPI/GDP/BoE + holidays (keyword-based)
# - Pause only for other HIGH-importance events (pre/post minutes)
# v2.6-rules
import os, json, time, hashlib, requests, re
from datetime import datetime, timedelta
from typing import Dict, List, Tuple, Set

NEWS_FILTERS_VERSION = "2.6-rules"

# ---------- helpers ----------
def _json_env(name: str, default):
    try:
        raw = os.environ.get(name, "")
        return default if not raw else json.loads(raw)
    except Exception:
        return default

def _safe_int_env(name: str, default: int) -> int:
    v = os.environ.get(name)
    try:
        return int(v) if v is not None else default
    except Exception:
        return default

def _safe_float_env(name: str, default: float) -> float:
    v = os.environ.get(name)
    try:
        return float(v) if v is not None else default
    except Exception:
        return default

def _dbg(msg: str):
    if os.environ.get("NEWS_DEBUG", "0") not in ("0","false","False",""):
        print(f"[DEBUG] [NEWS] {msg}")

def _norm_impact(x: str) -> str:
    x = (x or "").strip().lower()
    if x in ("high","3","important"): return "high"
    if x in ("medium","2","moderate"): return "medium"
    if x in ("low","1","minor"):       return "low"
    return x or "low"

# ---------- caching ----------
def _cache_path(base_dir: str, key: dict) -> str:
    os.makedirs(base_dir, exist_ok=True)
    blob = json.dumps(key, sort_keys=True).encode("utf-8")
    h = hashlib.sha1(blob).hexdigest()[:16]
    return os.path.join(base_dir, f"news_{h}.json")

def _load_cache(path: str, ttl_days: int):
    try:
        st = os.stat(path)
        age_days = (time.time() - st.st_mtime) / 86400.0
        if age_days > ttl_days:
            return None
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None

def _save_cache(path: str, rows):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False)
    except Exception:
        pass

# ---------- 429 handling ----------
def _wait_from_headers(headers) -> float:
    ra = headers.get("Retry-After")
    if ra:
        try:
            return max(0.0, float(ra))
        except Exception:
            pass
    return -1.0

# ---------- provider call ----------
def _fetch_investing_calendar(start_dt: datetime, end_dt: datetime,
                              countries: List[str], importances: List[str],
                              date_fmt: str) -> List[dict]:
    base     = (os.environ.get("RAPIDAPI_BASE", "") or "").rstrip("/")
    endpoint = os.environ.get("RAPIDAPI_ENDPOINT", "/news/economic-calendar")
    host     = os.environ.get("RAPIDAPI_HOST", "")
    key      = os.environ.get("RAPIDAPI_KEY", "")
    if not (base and endpoint and host and key):
        _dbg("RapidAPI credentials/host/base missing.")
        return []

    url = f"{base}/{endpoint.lstrip('/')}"
    headers = {"x-rapidapi-host": host, "x-rapidapi-key": key}

    params = {
        "from_date": start_dt.strftime(date_fmt),
        "to_date":   end_dt.strftime(date_fmt),
    }
    if countries:
        params["countries"] = ",".join([c.lower() for c in countries])
    if importances:
        params["importances"] = ",".join([i.lower() for i in importances])

    max_retries = _safe_int_env("NEWS_MAX_RETRIES_429", 4)
    base_delay  = _safe_float_env("NEWS_RETRY_BASE_DELAY_S", 1.5)

    attempt = 0
    while True:
        attempt += 1
        _dbg(f"RapidAPI request #{attempt} params={params}")
        r = requests.get(url, params=params, headers=headers, timeout=30)
        if r.status_code == 429:
            if attempt > max_retries:
                _dbg("RapidAPI 429: max retries reached.")
                return []
            wait = _wait_from_headers(r.headers)
            if wait < 0:
                wait = min(45.0, base_delay * (2 ** (attempt - 1)))
            _dbg(f"RapidAPI 429; sleeping {wait:.1f}s")
            time.sleep(wait)
            continue
        try:
            r.raise_for_status()
        except requests.HTTPError as e:
            _dbg(f"RapidAPI HTTP error: {e}")
            return []

        payload = r.json()
        rows = payload.get("data", payload) if isinstance(payload, dict) else payload
        if isinstance(rows, dict):
            for k in ("events","result","items"):
                if k in rows and isinstance(rows[k], list):
                    rows = rows[k]; break
        if not isinstance(rows, list):
            _dbg("RapidAPI: unexpected payload shape.")
            return []

        # Normalize to list of dicts: {"dt": datetime, "impact": "high/med/low", "title": str}
        events: List[dict] = []
        for row in rows:
            try:
                d = row.get("date")
                t = row.get("time") or "00:00"
                title = (row.get("event") or row.get("title") or "").strip()
                imp   = _norm_impact(row.get("importance") or row.get("impact") or "")
                dt = None
                for fmt in ("%d/%m/%Y %H:%M", "%d/%m/%Y",
                            "%Y-%m-%d %H:%M", "%Y-%m-%d",
                            "%m/%d/%Y %H:%M", "%m/%d/%Y"):
                    try:
                        dt = datetime.strptime(f"{d} {t}".strip(), fmt)
                        break
                    except Exception:
                        continue
                if not dt:
                    try:
                        dt = datetime.fromisoformat(f"{d} {t}".strip().replace("Z",""))
                    except Exception:
                        continue
                if start_dt <= dt <= end_dt:
                    events.append({"dt": dt, "impact": imp, "title": title})
            except Exception:
                continue

        _dbg(f"RapidAPI normalized events in range: {len(events)}")
        return events

# ---------- rules & builders ----------
def _compile_keyword_list(words: List[str]) -> List[re.Pattern]:
    pats = []
    for w in (words or []):
        try:
            # Escape plain strings; accept simple regex if user provides (?i) etc.
            pats.append(re.compile(w, re.IGNORECASE))
        except re.error:
            pats.append(re.compile(re.escape(w), re.IGNORECASE))
    return pats

def build_news_filters_for_backtest(start_dt: datetime, end_dt: datetime):
    """
    Returns:
      full_day_blackouts: Set['YYYY-MM-DD']
      pause_windows_by_date: Dict['YYYY-MM-DD', List[(start_dt, end_dt)]]
    Rules:
      - If title matches any FULLDAY keyword -> full-day skip
      - Else if impact in NEWS_PAUSE_IMPORTANCES -> pause window (pre/post minutes)
      - Otherwise: ignore
    """
    print(f"[INFO] [NEWS] news_filters version: {NEWS_FILTERS_VERSION}")

    # Inclusive end-of-day if only a date was provided
    if end_dt.hour == 0 and end_dt.minute == 0 and end_dt.second == 0:
        end_dt = end_dt + timedelta(days=1) - timedelta(seconds=1)

    countries        = _json_env("RAPIDAPI_COUNTRIES_JSON", ["united states","united kingdom","euro zone","germany"])
    fetch_imp        = _json_env("RAPIDAPI_IMPORTANCES_JSON", ["low","medium","high"])  # fetch all so we catch holidays
    pause_imp        = set(_json_env("NEWS_PAUSE_IMPORTANCES_JSON", ["high"]))         # only high → pause
    full_imp         = set(_json_env("NEWS_FULL_DAY_IMPORTANCES_JSON", []))            # empty by default
    full_kw          = _json_env("NEWS_FULLDAY_KEYWORDS_JSON", ["FOMC","CPI","GDP","BoE","Holiday"])
    extra_pause_kw   = _json_env("NEWS_KEYWORDS_PAUSE_JSON", [])
    pause_pre_m      = _safe_int_env("NEWS_PAUSE_PRE_MIN", 30)
    pause_post_m     = _safe_int_env("NEWS_PAUSE_POST_MIN", 30)

    date_fmt         = os.environ.get("RAPIDAPI_DATE_FMT", "%d/%m/%Y")
    cache_dir        = os.environ.get("NEWS_CACHE_DIR", "cache/news")
    ttl_days         = _safe_int_env("NEWS_CACHE_TTL_DAYS", 30)

    # Cache key
    cache_key = {
        "provider": "rapidapi_investing_ultimate",
        "from": start_dt.strftime("%Y-%m-%d %H:%M:%S"),
        "to":   end_dt.strftime("%Y-%m-%d %H:%M:%S"),
        "countries": countries,
        "importances": fetch_imp,
    }
    cpath = _cache_path(cache_dir, cache_key)
    cached = _load_cache(cpath, ttl_days)

    if cached is not None:
        _dbg(f"Using cached calendar: {len(cached)} events")
        events = cached
    else:
        events = _fetch_investing_calendar(start_dt, end_dt, countries, fetch_imp, date_fmt)
        if events:
            _save_cache(cpath, events)
        else:
            _dbg("Provider returned 0 events; proceeding with no news filters.")
            return set(), {}

    # Compile keyword patterns
    fullday_pats = _compile_keyword_list(full_kw + ["Bank Holiday","Public Holiday","Market Holiday","National Holiday"])
    pause_pats   = _compile_keyword_list(extra_pause_kw)

    full_days: Set[str] = set()
    pauses: Dict[str, List[Tuple[datetime, datetime]]] = {}

    for ev in events:
        dt     = ev["dt"]
        day_key = dt.date().isoformat()
        title  = (ev.get("title") or "")
        impact = _norm_impact(ev.get("impact"))

        title_l = title.lower()

        # ---- FULL DAY by keywords OR (optionally) by impact in full_imp ----
        if any(p.search(title) for p in fullday_pats) or (impact in full_imp):
            full_days.add(day_key)
            continue

        # ---- PAUSE for high-importance or extra pause keywords ----
        if (impact in pause_imp) or any(p.search(title) for p in pause_pats):
            s = dt - timedelta(minutes=pause_pre_m)
            e = dt + timedelta(minutes=pause_post_m)
            pauses.setdefault(day_key, []).append((s, e))
            continue

        # else: ignore this event

    _dbg(f"Built pauses={len(pauses)} day(s); full-day blackouts={len(full_days)}")
    return full_days, pauses

def bar_blocked_by_news(bar_time: datetime, full_day_blackouts, pause_windows_by_date) -> bool:
    """True if bar_time falls in a full-day blackout or any pause window."""
    day_key = bar_time.date().isoformat()

    if not isinstance(full_day_blackouts, set):
        full_day_blackouts = set(full_day_blackouts or [])
    if day_key in full_day_blackouts:
        return True

    pauses = pause_windows_by_date
    if not isinstance(pauses, dict):
        tmp: Dict[str, List[Tuple[datetime, datetime]]] = {}
        for win in (pauses or []):
            if not win: continue
            s, e = win
            tmp.setdefault(s.date().isoformat(), []).append((s, e))
        pauses = tmp

    for s, e in pauses.get(day_key, []):
        if s <= bar_time <= e:
            return True
    return False
