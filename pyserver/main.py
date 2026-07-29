"""FastAPI sidecar wrapping easy-tdx (通达信) + Tushare Pro + AkShare.

Data-source split:
- A-share (sh/sz/bj): easy-tdx (direct 通达信 TCP, no token/quota) is the
  preferred source for klines and realtime spot quotes, with the Tushare/
  AkShare paths kept as automatic fallbacks. AkShare's Eastmoney snapshot
  remains the source for spot/fundamental PE/PB/market-cap; Tushare Pro
  remains the fallback kline source plus daily_basic / report_rc.
- HK: akshare's stock_hk_hist — Tushare's hk_daily is hard-capped at
  10 calls/day on the free Pro tier (and 2/min within that), making it
  unusable for a HK watchlist beyond the first ~10 requests of the day.

All responses write through a SQLite cache so upstream is hit at most once
per symbol per trading day (klines/fundamentals/analyst) or per 30s (spot).
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger("pyserver")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)

import akshare as ak
import pandas as pd
import requests
import tushare as ts
from dotenv import load_dotenv
from easy_tdx import Adjust, MacClient, Market, Period
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel
from tushare.pro import client as ts_client

# ---------- bootstrap ------------------------------------------------------

load_dotenv(Path(__file__).parent / ".env")
TUSHARE_TOKEN = os.environ.get("TUSHARE_TOKEN")
TUSHARE_HTTP_URL = os.environ.get("TUSHARE_HTTP_URL")
if not TUSHARE_TOKEN:
    raise RuntimeError(
        "TUSHARE_TOKEN not set. Put it in pyserver/.env or export it.",
    )


def _patch_tushare_http_url(url: str, target: Any) -> list[str]:
    patched: list[str] = []
    for attr in dir(target):
        if "http_url" in attr or attr.endswith("__url"):
            try:
                setattr(target, attr, url)
                patched.append(attr)
            except Exception:
                pass
    return patched


if TUSHARE_HTTP_URL:
    patched_attrs = _patch_tushare_http_url(TUSHARE_HTTP_URL, ts_client.DataApi)
    if not patched_attrs:
        raise RuntimeError(
            f"Unable to patch Tushare proxy URL for tushare {ts.__version__}",
        )
ts.set_token(TUSHARE_TOKEN)
_pro = ts.pro_api()
if TUSHARE_HTTP_URL:
    _patch_tushare_http_url(TUSHARE_HTTP_URL, _pro)

DB_PATH = Path(os.environ.get("PYSERVER_DB_PATH", Path(__file__).parent / "cache.db"))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

# WAL once at startup: the /spots batch fans out across threads, and the
# default rollback journal serializes concurrent readers against one writer.
with sqlite3.connect(DB_PATH) as _bootstrap_conn:
    _bootstrap_conn.execute("PRAGMA journal_mode=WAL")

app = FastAPI(title="a-share-assistant pyserver", version="0.3.0")


@app.middleware("http")
async def _request_logging_middleware(request, call_next):
    """Log every request with method, path, status and duration."""
    start = time.monotonic()
    try:
        response = await call_next(request)
    except Exception as exc:
        elapsed_ms = (time.monotonic() - start) * 1000
        logger.error(
            "request %s %s -> 500 (%.0fms) error=%s: %s",
            request.method, request.url.path, elapsed_ms,
            type(exc).__name__, exc,
        )
        raise
    elapsed_ms = (time.monotonic() - start) * 1000
    level = logging.WARNING if response.status_code >= 400 else logging.INFO
    logger.log(
        level,
        "request %s %s -> %d (%.0fms)",
        request.method, request.url.path, response.status_code, elapsed_ms,
    )
    return response


# ---------- cache ----------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS cache (
  key TEXT PRIMARY KEY,
  payload TEXT NOT NULL,
  fetched_at INTEGER NOT NULL,
  ttl_seconds INTEGER NOT NULL
);
"""


@contextmanager
def db():
    # busy_timeout guards against "database is locked" when the /spots batch
    # thread pool writes concurrently with an incoming read.
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute(SCHEMA)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def cache_get(key: str) -> Any | None:
    with db() as conn:
        row = conn.execute(
            "SELECT payload, fetched_at, ttl_seconds FROM cache WHERE key = ?",
            (key,),
        ).fetchone()
    if not row:
        return None
    payload, fetched_at, ttl = row
    if ttl > 0 and time.time() - fetched_at > ttl:
        return None
    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        # A corrupt row would otherwise 500 every request hitting this key.
        logger.warning("cache corrupt payload, purging key=%s", key)
        with db() as conn:
            conn.execute("DELETE FROM cache WHERE key = ?", (key,))
        return None


def cache_put(key: str, value: Any, ttl_seconds: int) -> None:
    with db() as conn:
        conn.execute(
            "REPLACE INTO cache (key, payload, fetched_at, ttl_seconds) VALUES (?, ?, ?, ?)",
            (key, json.dumps(value, ensure_ascii=False), int(time.time()), ttl_seconds),
        )


def cache_update_keep_age(key: str, value: Any) -> None:
    """Rewrite a cached payload without resetting its TTL clock.

    Re-persisting via cache_put bumps fetched_at, so an entry that is read
    at least once per TTL window would never expire.
    """
    with db() as conn:
        conn.execute(
            "UPDATE cache SET payload = ? WHERE key = ?",
            (json.dumps(value, ensure_ascii=False), key),
        )


def seconds_until_next_trading_close() -> int:
    """TTL so daily klines refresh after the next 15:30 CN market close."""
    now = datetime.now()
    target = now.replace(hour=15, minute=30, second=0, microsecond=0)
    if now >= target:
        target += timedelta(days=1)
    # Daily bars don't change over the weekend; a TTL expiring on Sat/Sun
    # would force a pointless refetch of an identical series.
    while target.weekday() >= 5:  # Saturday=5, Sunday=6
        target += timedelta(days=1)
    return int((target - now).total_seconds())


def _norm_symbol(symbol: str) -> str:
    """Cache-key normalization so `SH600519` and `sh600519` share one entry."""
    return symbol.strip().lower()


# ---------- retry wrapper + per-endpoint rate limiter ----------------------

import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed


class _TokenBucket:
    """Simple token bucket — at most `n` calls per `window_s` seconds."""

    def __init__(self, n: int, window_s: float) -> None:
        self.n = n
        self.window = window_s
        self.calls: deque[float] = deque()
        self.lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self.lock:
                now = time.monotonic()
                while self.calls and now - self.calls[0] > self.window:
                    self.calls.popleft()
                if len(self.calls) < self.n:
                    self.calls.append(now)
                    return
                wait = self.window - (now - self.calls[0]) + 0.05
            time.sleep(wait)


# Tushare free tier caps hk_daily at 2/minute. Self-throttle to avoid 502s.
_HK_DAILY_LIMITER = _TokenBucket(n=2, window_s=65)
_REPORT_RC_LIMITER = _TokenBucket(n=2, window_s=65)
_SPOT_BATCH_CONCURRENCY = int(os.environ.get("SPOT_BATCH_CONCURRENCY", 2))
_SOURCE_STATUS_LOCK = threading.Lock()
_SOURCE_STATUS: dict[str, dict[str, Any]] = {}


def _mark_source(source: str, ok: bool, message: str | None = None) -> None:
    with _SOURCE_STATUS_LOCK:
        _SOURCE_STATUS[source] = {
            "ok": ok,
            "checked_at": datetime.now().isoformat(),
            **({"message": message[:240]} if message else {}),
        }


def _source_status_snapshot() -> dict[str, dict[str, Any]]:
    with _SOURCE_STATUS_LOCK:
        return {k: dict(v) for k, v in _SOURCE_STATUS.items()}


def _with_retries(fn, *args, attempts: int = 3, base_delay: float = 0.5, **kwargs):
    fn_name = getattr(fn, "__name__", repr(fn))
    last: Exception | None = None
    for i in range(attempts):
        try:
            return fn(*args, **kwargs)
        except Exception as e:  # noqa: BLE001
            last = e
            logger.warning(
                "retry %d/%d fn=%s error=%s: %s",
                i + 1, attempts, fn_name, type(e).__name__, e,
            )
            time.sleep(base_delay * (2 ** i))
    assert last is not None
    logger.error("all %d retries exhausted fn=%s error=%s: %s", attempts, fn_name, type(last).__name__, last)
    raise last


def _hk_daily(**kwargs):
    """Rate-limited wrapper around pro.hk_daily."""
    _HK_DAILY_LIMITER.acquire()
    return _pro.hk_daily(**kwargs)


def _report_rc(**kwargs):
    """Rate-limited wrapper around pro.report_rc."""
    _REPORT_RC_LIMITER.acquire()
    return _pro.report_rc(**kwargs)


# ---------- easy-tdx (通达信) A-share datasource ----------------------------
# Direct TCP to a 通达信 quote server: no token, no daily quota. Preferred for
# A-share klines and realtime spot quotes; the Tushare/AkShare paths below are
# automatic fallbacks. HK is unaffected and still served by AkShare.

_TDX_MARKET = {"sh": Market.SH, "sz": Market.SZ, "bj": Market.BJ}
_TDX_ADJUST = {"": Adjust.NONE, "qfq": Adjust.QFQ, "hfq": Adjust.HFQ}

# A single socket speaks the TDX request/response protocol, so every call is
# serialized through this lock (the /spots batch fans out across threads).
_TDX_LOCK = threading.Lock()
_tdx_client: MacClient | None = None
_tdx_down_until = 0.0  # monotonic deadline; skip (re)connect attempts until then


def _tdx_call(method: str, *args, **kwargs):
    """Call a MacClient method under the lock, reconnecting once on failure.

    Returns None when the upstream is unreachable so callers fall back to the
    existing Tushare/AkShare path instead of erroring.
    """
    global _tdx_client, _tdx_down_until
    with _TDX_LOCK:
        for attempt in range(2):
            if _tdx_client is None:
                if time.monotonic() < _tdx_down_until:
                    return None
                try:
                    _tdx_client = MacClient.from_best_host(ping_timeout=3.0)
                except Exception as e:
                    logger.error("easy_tdx connect failed: %s: %s", type(e).__name__, e)
                    _mark_source("easy_tdx", False, "connect failed")
                    _tdx_down_until = time.monotonic() + 120
                    return None
            try:
                result = getattr(_tdx_client, method)(*args, **kwargs)
                _mark_source("easy_tdx", True)
                return result
            except Exception as e:
                logger.error(
                    "easy_tdx call failed method=%s attempt=%d error=%s: %s",
                    method, attempt + 1, type(e).__name__, e,
                )
                _mark_source("easy_tdx", False, str(e))
                try:
                    _tdx_client.close()
                except Exception:
                    pass
                _tdx_client = None  # retry once with a fresh connection
        _tdx_down_until = time.monotonic() + 60
        return None


def _tdx_klines(
    market: str, ts_code: str, start: str, end: str, adjust: str
) -> list[dict[str, Any]] | None:
    """A-share daily klines via easy-tdx, filtered to [start, end] (YYYYMMDD)."""
    mkt = _TDX_MARKET.get(market)
    if mkt is None:
        return None
    # get_stock_kline returns the most-recent `count` bars; size the request
    # from the requested window (≈242 trading days/year) so a multi-year range
    # is still fully covered, with headroom and a sane ceiling.
    try:
        start_d = datetime.strptime(start, "%Y%m%d").date()
    except ValueError:
        start_d = date.today() - timedelta(days=365)
    span_days = max((date.today() - start_d).days, 1)
    count = min(int(span_days * 0.72) + 30, 2000)
    df = _tdx_call(
        "get_stock_kline", mkt, _compact_code(ts_code), Period.DAILY,
        count=count, adjust=_TDX_ADJUST.get(adjust, Adjust.NONE),
    )
    if df is None or df.empty:
        return None
    # get_stock_kline returns the most-recent `count` bars. If upstream filled
    # the entire request and the earliest bar still lands well after `start`,
    # older bars were cut off (e.g. the 2000-bar ceiling on a multi-year
    # window) — return None so the Tushare fallback serves the full history
    # instead of caching a silently truncated series.
    if len(df) >= count:
        earliest = pd.Timestamp(df["datetime"].min()).strftime("%Y%m%d")
        if earliest > (start_d + timedelta(days=10)).strftime("%Y%m%d"):
            return None
    rows: list[dict[str, Any]] = []
    for r in df.itertuples():
        d = pd.Timestamp(r.datetime).strftime("%Y-%m-%d")
        ymd = d.replace("-", "")
        if ymd < start or ymd > end:
            continue
        rows.append({
            "date": d,
            "open": float(r.open),
            "high": float(r.high),
            "low": float(r.low),
            "close": float(r.close),
            # TDX klines report raw shares; Tushare pro_bar (the fallback) and
            # the rest of the API use 手 (lots = 100 shares). Normalize so the
            # volume field is identical regardless of which source served it.
            "volume": float(r.vol) / 100,
            # easy-tdx turnover is denominated in yuan.
            "amount": float(r.amount),
        })
    return rows or None


def _tdx_spot(symbol: str, ts_code: str, market: str) -> dict[str, Any] | None:
    """Realtime A-share quote via easy-tdx (live price, not last close)."""
    mkt = _TDX_MARKET.get(market)
    if mkt is None:
        return None
    df = _tdx_call("get_stock_quotes", [(mkt, _compact_code(ts_code))])
    if df is None or df.empty:
        return None
    r = df.iloc[0]
    price = _num_or_none(r.get("close"))
    if price is None or price <= 0:
        return None
    pre_close = _num_or_none(r.get("pre_close"))
    change_pct = round((price / pre_close - 1) * 100, 2) if pre_close and pre_close > 0 else 0
    # TDX names can carry padding spaces (e.g. "五 粮 液"); collapse them.
    name = "".join(str(r.get("name") or "").split()) or _resolve_name(ts_code, market) or ""
    return {
        "symbol": symbol,
        "name": name,
        "price": round(price, 3),
        "change_pct": change_pct,
        "volume": _num_or_none(r.get("vol")) or 0,
        "turnover": _num_or_none(r.get("amount")) or 0,
        "source": "easy_tdx",
        "as_of": datetime.now().isoformat(),
    }


def _latest_profit_yoy(ts_code: str) -> float | None:
    """Return the latest available net-profit growth percentage for PEG."""
    start = (date.today() - timedelta(days=540)).strftime("%Y%m%d")
    today = date.today().strftime("%Y%m%d")
    df = _with_retries(
        _pro.fina_indicator,
        ts_code=ts_code,
        start_date=start,
        end_date=today,
        fields="ts_code,ann_date,end_date,netprofit_yoy,q_netprofit_yoy,q_profit_yoy",
    )
    if df is None or df.empty:
        return None
    df = df.sort_values(["end_date", "ann_date"], na_position="first")
    latest = df.iloc[-1]
    for col in ("netprofit_yoy", "q_netprofit_yoy", "q_profit_yoy"):
        value = _num_or_none(latest.get(col))
        if value is not None:
            return value
    return None


def _attach_profit_yoy(out: dict[str, Any], ts_code: str, market: str) -> None:
    if market == "hk":
        return
    try:
        profit_yoy = _latest_profit_yoy(ts_code)
    except Exception as e:
        logger.warning("profit_yoy fetch failed ts_code=%s error=%s: %s", ts_code, type(e).__name__, e)
        return
    if profit_yoy is not None:
        out["profit_yoy"] = profit_yoy


# ---------- models ---------------------------------------------------------


class Kline(BaseModel):
    date: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    amount: float | None = None


class MinuteKline(BaseModel):
    time: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    amount: float


class MinuteKlineSeries(BaseModel):
    symbol: str
    ts_code: str
    freq: str
    source: str
    realtime: bool
    bars: list[MinuteKline]


class Fundamental(BaseModel):
    symbol: str
    name: str | None = None
    pe_ttm: float | None = None
    pb: float | None = None
    market_cap: float | None = None  # 亿元
    revenue_yoy: float | None = None
    profit_yoy: float | None = None


class Analyst(BaseModel):
    symbol: str
    buy_count: int | None = None
    total_count: int | None = None
    buy_ratio: float | None = None
    consensus_eps_next: float | None = None
    implied_target: float | None = None
    target_price_source: str | None = None
    target_price_method: str | None = None
    target_price_confidence: float | None = None
    target_horizon_days: int | None = None
    current_price: float | None = None
    current_price_source: str | None = None
    current_price_as_of: str | None = None
    upside_pct: float | None = None


# ---------- symbol normalization -------------------------------------------


def _to_ts_code(symbol: str) -> tuple[str, str]:
    """Convert internal symbol -> (ts_code, market). market in {sh, sz, bj, hk}."""
    s = symbol.lower().strip()
    if s.startswith(("sh", "sz", "bj")):
        code, mkt = s[2:], s[:2]
    elif s.startswith("hk"):
        code, mkt = s[2:].zfill(5), "hk"
    elif s.startswith("920"):
        # BSE listings issued since late 2024; must precede the "9" → SH
        # branch (which is for 900xxx Shanghai B-shares).
        code, mkt = s, "bj"
    elif s.startswith(("60", "68", "9")):
        code, mkt = s, "sh"
    elif s.startswith(("00", "30", "20")):
        code, mkt = s, "sz"
    elif s.startswith(("8", "4")):
        code, mkt = s, "bj"
    else:
        code, mkt = s.zfill(5), "hk"
    suffix = {"sh": ".SH", "sz": ".SZ", "bj": ".BJ", "hk": ".HK"}[mkt]
    return code + suffix, mkt


# Tushare expects YYYYMMDD; the route accepts both forms.
def _date(s: str) -> str:
    s = s.replace("-", "")
    return s


_DATE_8 = re.compile(r"^\d{8}$")
_MINUTE_FREQS = ("1min", "5min", "15min", "30min", "60min")
_MINUTE_RANGE_LIMIT = timedelta(days=31)


def _checked_date(s: str, param: str) -> str:
    """Normalize and validate a date param; 400 instead of an upstream 502."""
    compact = _date(s)
    if not _DATE_8.match(compact):
        raise HTTPException(400, f"invalid {param} date: {s!r} (want YYYYMMDD)")
    return compact


def _minute_datetime(value: str, param: str, *, end_of_day: bool) -> datetime:
    normalized = value.strip().replace("T", " ")
    date_only = False
    for fmt in ("%Y%m%d", "%Y-%m-%d", "%Y-%m-%d %H:%M:%S"):
        try:
            parsed = datetime.strptime(normalized, fmt)
            date_only = fmt != "%Y-%m-%d %H:%M:%S"
            break
        except ValueError:
            continue
    else:
        raise HTTPException(
            400,
            f"invalid {param}: {value!r} "
            "(want YYYYMMDD, YYYY-MM-DD, or YYYY-MM-DD HH:MM:SS)",
        )
    if date_only:
        return parsed.replace(
            hour=15 if end_of_day else 9,
            minute=0 if end_of_day else 30,
        )
    return parsed


def _checked_minute_range(start: str, end: str | None) -> tuple[datetime, datetime]:
    start_dt = _minute_datetime(start, "start", end_of_day=False)
    if end is None:
        end_dt = start_dt.replace(hour=15, minute=0, second=0)
    else:
        end_dt = _minute_datetime(end, "end", end_of_day=True)
    if start_dt > end_dt:
        raise HTTPException(400, f"start {start!r} is after end {end!r}")
    if end_dt - start_dt > _MINUTE_RANGE_LIMIT:
        raise HTTPException(400, "minute kline range cannot exceed 31 calendar days")
    return start_dt, end_dt


def _num_or_none(value: Any) -> float | None:
    if value is None or pd.isna(value):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    matches = re.findall(r"-?\d+(?:\.\d+)?", str(value))
    if not matches:
        return None
    nums = [float(x) for x in matches]
    return sum(nums) / len(nums)


def _compact_code(ts_code: str) -> str:
    return ts_code.split(".")[0]


def _ak_col(row: pd.Series, *names: str) -> Any:
    for name in names:
        if name in row and pd.notna(row.get(name)):
            return row.get(name)
    return None


def _market_cap_to_yi(value: float | None) -> float | None:
    if value is None:
        return None
    # AkShare's Eastmoney spot endpoint reports market cap in yuan. Keep this
    # defensive in case an alternate backend already returns 亿元.
    if abs(value) > 1_000_000:
        return value / 1e8
    return value


def _eastmoney_market_code(market: str) -> int:
    # Eastmoney uses 1 for Shanghai and 0 for Shenzhen/Beijing in these quote
    # endpoints.
    return 1 if market == "sh" else 0


def _ak_a_spot_rows(ts_code: str, market: str) -> dict[str, Any] | None:
    """Fetch/cached A-share spot quote with a hard timeout.

    AkShare's whole-market spot helpers paginate thousands of rows and can take
    tens of seconds. This mirrors the single-symbol Eastmoney endpoint used by
    AkShare so a slow upstream can fall back to Tushare quickly.
    """
    code = _compact_code(ts_code)
    key = f"ak:a:spot:em:{code}"
    cached = cache_get(key)
    if cached is not None:
        # A cached miss marker means upstream failed moments ago; back off
        # instead of re-stalling on the 3s request timeout. (Caching a bare
        # None would be indistinguishable from a cache miss.)
        return None if cached.get("__miss__") else cached
    url = "https://push2.eastmoney.com/api/qt/stock/get"
    params = {
        "fltt": "2",
        "invt": "2",
        "fields": "f43,f57,f58,f116,f117,f162,f163,f167,f168,f47,f48,f170",
        "secid": f"{_eastmoney_market_code(market)}.{code}",
    }
    try:
        response = requests.get(url, params=params, timeout=3)
        response.raise_for_status()
        data = response.json().get("data")
        _mark_source("eastmoney_push2", True)
    except Exception as e:
        logger.warning("eastmoney_push2 fetch failed code=%s error=%s: %s", code, type(e).__name__, e)
        _mark_source("eastmoney_push2", False, str(e))
        cache_put(key, {"__miss__": True}, 10)
        return None
    if not data:
        _mark_source("eastmoney_push2", False, "empty response")
        cache_put(key, {"__miss__": True}, 10)
        return None
    row = {
        "代码": data.get("f57") or code,
        "名称": data.get("f58"),
        "最新价": data.get("f43"),
        "涨跌幅": data.get("f170"),
        "成交量": data.get("f47"),
        "成交额": data.get("f48"),
        "总市值": data.get("f116"),
        "流通市值": data.get("f117"),
        "市盈率-动态": data.get("f162"),
        "市盈率-TTM": data.get("f163"),
        "市净率": data.get("f167"),
        "换手率": data.get("f168"),
    }
    cache_put(key, row, 30)
    return row


def _ak_a_spot(ts_code: str, market: str) -> dict[str, Any] | None:
    if market not in {"sh", "sz", "bj"}:
        return None
    try:
        return _ak_a_spot_rows(ts_code, market)
    except Exception as e:
        logger.warning("ak_a_spot failed ts_code=%s error=%s: %s", ts_code, type(e).__name__, e)
        return None


def _spot_price_from_ak(row: dict[str, Any]) -> float | None:
    return _num_or_none(row.get("最新价"))


def _spot_change_pct_from_ak(row: dict[str, Any]) -> float | None:
    return _num_or_none(row.get("涨跌幅"))


def _market_prefix(market: str) -> str | None:
    if market == "sh":
        return "sh"
    if market == "sz":
        return "sz"
    return None


def _tencent_spot(symbol: str, ts_code: str, market: str) -> dict[str, Any] | None:
    """Fallback realtime quote via Tencent's public quote endpoint."""
    prefix = _market_prefix(market)
    if prefix is None:
        return None
    code = _compact_code(ts_code)
    try:
        r = requests.get(
            "https://qt.gtimg.cn/q=" + prefix + code,
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=3,
        )
        r.raise_for_status()
        text = r.text.strip()
        payload = text.split('"', 2)[1] if '"' in text else ""
        parts = payload.split("~")
        if len(parts) < 5:
            _mark_source("tencent_quote", False, "short response")
            return None
        price = _num_or_none(parts[3])
        pre_close = _num_or_none(parts[4])
        if price is None or price <= 0:
            _mark_source("tencent_quote", False, "missing price")
            return None
        _mark_source("tencent_quote", True)
        return {
            "symbol": symbol,
            "name": "".join((parts[1] or "").split()) or _resolve_name(ts_code, market) or "",
            "price": round(price, 3),
            "change_pct": round((price / pre_close - 1) * 100, 2) if pre_close and pre_close > 0 else 0,
            "volume": _num_or_none(parts[6] if len(parts) > 6 else None) or 0,
            "turnover": _num_or_none(parts[37] if len(parts) > 37 else None) or 0,
            "source": "tencent_quote",
            "as_of": datetime.now().isoformat(),
        }
    except Exception as e:
        logger.warning("tencent_quote failed symbol=%s error=%s: %s", symbol, type(e).__name__, e)
        _mark_source("tencent_quote", False, str(e))
        return None


def _sina_spot(symbol: str, ts_code: str, market: str) -> dict[str, Any] | None:
    """Fallback realtime quote via Sina's public quote endpoint."""
    prefix = _market_prefix(market)
    if prefix is None:
        return None
    code = _compact_code(ts_code)
    try:
        r = requests.get(
            "https://hq.sinajs.cn/list=" + prefix + code,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Referer": "https://finance.sina.com.cn/",
            },
            timeout=3,
        )
        r.raise_for_status()
        r.encoding = "gbk"
        text = r.text.strip()
        payload = text.split('"', 2)[1] if '"' in text else ""
        parts = payload.split(",")
        if len(parts) < 32:
            _mark_source("sina_quote", False, "short response")
            return None
        price = _num_or_none(parts[3])
        pre_close = _num_or_none(parts[2])
        if price is None or price <= 0:
            _mark_source("sina_quote", False, "missing price")
            return None
        _mark_source("sina_quote", True)
        trade_date = parts[30] if len(parts) > 30 else ""
        trade_time = parts[31] if len(parts) > 31 else ""
        return {
            "symbol": symbol,
            "name": "".join((parts[0] or "").split()) or _resolve_name(ts_code, market) or "",
            "price": round(price, 3),
            "change_pct": round((price / pre_close - 1) * 100, 2) if pre_close and pre_close > 0 else 0,
            "volume": _num_or_none(parts[8]) or 0,
            "turnover": _num_or_none(parts[9]) or 0,
            "source": "sina_quote",
            "as_of": f"{trade_date}T{trade_time}" if trade_date and trade_time else datetime.now().isoformat(),
        }
    except Exception as e:
        logger.warning("sina_quote failed symbol=%s error=%s: %s", symbol, type(e).__name__, e)
        _mark_source("sina_quote", False, str(e))
        return None


def _ak_consensus_eps(symbol: str) -> tuple[float | None, int | None]:
    """Fetch nearest annual EPS forecast from 同花顺 via akshare."""
    try:
        df = _with_retries(
            ak.stock_profit_forecast_ths,
            symbol=symbol,
            indicator="预测年报每股收益",
            attempts=2,
            base_delay=0.2,
        )
    except Exception as e:
        logger.warning("consensus_eps fetch failed symbol=%s error=%s: %s", symbol, type(e).__name__, e)
        return None, None
    if df is None or df.empty or "年度" not in df.columns or "均值" not in df.columns:
        return None, None

    current_year = date.today().year
    work = df.copy()
    work["年度"] = pd.to_numeric(work["年度"], errors="coerce")
    work["均值"] = pd.to_numeric(work["均值"], errors="coerce")
    work = work.dropna(subset=["年度", "均值"])
    work = work[work["年度"].astype(int) >= current_year]
    if work.empty:
        return None, None

    row = work.sort_values("年度").iloc[0]
    count = None
    if "预测机构数" in row and pd.notna(row.get("预测机构数")):
        count = int(row["预测机构数"])
    return round(float(row["均值"]), 4), count


def _ak_research_consensus(symbol: str) -> dict[str, Any]:
    """Fetch per-stock research reports from Eastmoney via akshare."""
    try:
        df = _with_retries(
            ak.stock_research_report_em,
            symbol=symbol,
            attempts=2,
            base_delay=0.2,
        )
    except Exception as e:
        logger.warning("research_consensus fetch failed symbol=%s error=%s: %s", symbol, type(e).__name__, e)
        return {}
    if df is None or df.empty:
        return {}

    out: dict[str, Any] = {"total_count": int(len(df))}

    if "东财评级" in df.columns:
        ratings = df["东财评级"].fillna("").astype(str)
        bullish = ratings.isin(["买入", "推荐", "强烈推荐", "增持"]).sum()
        out["buy_count"] = int(bullish)
        out["buy_ratio"] = round(out["buy_count"] / out["total_count"], 3)

    current_year = date.today().year
    eps_cols: list[tuple[int, str]] = []
    for col in df.columns:
        m = re.match(r"^(\d{4})-盈利预测-收益$", str(col))
        if m and int(m.group(1)) >= current_year:
            eps_cols.append((int(m.group(1)), str(col)))

    if eps_cols:
        _, eps_col = sorted(eps_cols)[0]
        eps_series = pd.to_numeric(df[eps_col], errors="coerce").dropna()
        if not eps_series.empty:
            out["consensus_eps_next"] = round(float(eps_series.median()), 4)

    targets = _extract_target_prices(df)
    if targets:
        out["implied_target"] = round(float(pd.Series(targets).median()), 3)
        out["target_price_source"] = "akshare_eastmoney_explicit"

    return out


# Explicit sell-side targets can still be malformed because upstream schemas
# drift. A target implying more than +200% upside is treated as bad data.
MAX_IMPLIED_UPSIDE_RATIO = 2.0
MAX_TARGET_PRICE_YUAN = 10_000


def _extract_target_prices(df: pd.DataFrame) -> list[float]:
    """Extract only explicit per-share target-price columns.

    Do not infer targets from EPS or PE. The UI labels this as analyst target
    price, so the source must expose a target-price field directly.
    """
    target_cols: list[str] = []
    for col in df.columns:
        raw = str(col).strip()
        normalized = raw.lower()
        if normalized in {"target_price", "target", "tp"} or "目标价" in raw or "目标价格" in raw:
            target_cols.append(str(col))

    targets: list[float] = []
    for col in target_cols:
        targets.extend(x for x in (_num_or_none(v) for v in df[col]) if x is not None and x > 0)
    return targets


def _sanitize_analyst_payload(out: dict[str, Any]) -> dict[str, Any]:
    out = dict(out)
    source = out.get("target_price_source")
    target = _num_or_none(out.get("implied_target"))
    current_price = _num_or_none(out.get("current_price"))
    if not source or target is None or target <= 0 or target > MAX_TARGET_PRICE_YUAN:
        out["implied_target"] = None
        out["upside_pct"] = None
        out["target_price_source"] = None
        out["target_price_method"] = None
        out["target_price_confidence"] = None
        out["target_horizon_days"] = None
        return out
    if (
        current_price is not None
        and current_price > 0
        and target / current_price > 1 + MAX_IMPLIED_UPSIDE_RATIO
    ):
        out["implied_target"] = None
        out["upside_pct"] = None
        out["target_price_source"] = None
        out["target_price_method"] = None
        out["target_price_confidence"] = None
        out["target_horizon_days"] = None
        return out
    out["implied_target"] = target
    if current_price is not None and current_price > 0:
        out["upside_pct"] = round((target / current_price - 1) * 100, 2)
    return out


MODEL_TARGET_SOURCE = "model_atr_momentum_v1"
MODEL_TARGET_METHOD = (
    "15-30日规则目标：现价 + max(ATR14倍数, 前高突破空间, 动量项)，"
    "18%封顶；非券商目标价"
)
DYNAMIC_ANALYST_FIELDS = {
    "current_price",
    "current_price_source",
    "current_price_as_of",
    "upside_pct",
    "target_price_method",
    "target_price_confidence",
    "target_horizon_days",
}


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _technical_model_target(
    rows: list[dict[str, Any]],
    current_price: float | None,
) -> dict[str, Any] | None:
    """Short-horizon rule target from price action, ATR and resistance.

    This is an execution target, not sell-side fair value. It deliberately uses
    only auditable market data and is withheld when the setup is not
    constructive or the latest move is already too extended.
    """
    if current_price is None or current_price <= 0 or len(rows) < 25:
        return None
    clean = sorted(
        (r for r in rows if _num_or_none(r.get("close")) is not None),
        key=lambda r: str(r.get("date", "")),
    )
    if len(clean) < 25:
        return None

    closes = [float(_num_or_none(r.get("close")) or 0) for r in clean]
    highs = [float(_num_or_none(r.get("high")) or closes[i]) for i, r in enumerate(clean)]
    lows = [float(_num_or_none(r.get("low")) or closes[i]) for i, r in enumerate(clean)]

    closes[-1] = current_price
    highs[-1] = max(highs[-1], current_price)
    lows[-1] = min(lows[-1], current_price)

    current = closes[-1]
    previous = closes[-2]
    if previous <= 0 or closes[-6] <= 0 or closes[-21] <= 0:
        return None

    ma5 = _mean(closes[-5:])
    ma10 = _mean(closes[-10:])
    ma20 = _mean(closes[-20:])
    prior_high20 = max(closes[-21:-1])
    one_day_return = current / previous - 1
    momentum5 = current / closes[-6] - 1
    momentum20 = current / closes[-21] - 1

    true_ranges: list[float] = []
    for i in range(1, len(closes)):
        prev_close = closes[i - 1]
        if prev_close <= 0:
            continue
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - prev_close),
            abs(lows[i] - prev_close),
        )
        true_ranges.append(tr / prev_close)
    if len(true_ranges) < 14:
        return None
    atr_pct = _clamp(_mean(true_ranges[-14:]), 0.015, 0.12)

    breakout = current >= prior_high20 * 0.995 and momentum20 > 0
    pullback_turn = current >= ma10 and current <= ma5 * 1.04 and ma5 >= ma10 and momentum5 > -0.02
    trend_hold = current > ma10 and ma5 >= ma10 and ma10 >= ma20 and momentum20 > 0
    if not (breakout or pullback_turn or trend_hold):
        return None

    # Do not manufacture an attractive target after a chase-risk move.
    if one_day_return > 0.07 or momentum5 > 0.28:
        return None

    atr_component = (2.0 if breakout else 1.6 if pullback_turn else 1.35) * atr_pct
    resistance_component = max(0, prior_high20 / current - 1) + 0.8 * atr_pct
    momentum_component = max(0, momentum20) * 0.35 + max(0, momentum5) * 0.25
    upside = max(0.04, atr_component, resistance_component, momentum_component)
    upside = _clamp(upside, 0.04, 0.18)
    if one_day_return > 0.05 or momentum5 > 0.18:
        upside = min(upside, 0.08)

    confidence = 0.45
    confidence += 0.15 if breakout else 0
    confidence += 0.12 if pullback_turn else 0
    confidence += 0.10 if trend_hold else 0
    confidence += _clamp(momentum20, 0, 0.2) * 0.8
    confidence -= 0.12 if one_day_return > 0.05 else 0

    return {
        "target": round(current * (1 + upside), 3),
        "confidence": round(_clamp(confidence, 0.35, 0.9), 3),
    }


def _cacheable_analyst_payload(out: dict[str, Any]) -> dict[str, Any]:
    cached = dict(out)
    for field in DYNAMIC_ANALYST_FIELDS:
        cached.pop(field, None)
    if str(cached.get("target_price_source") or "").startswith("model_"):
        cached["implied_target"] = None
        cached["target_price_source"] = None
    return _sanitize_analyst_payload(cached)


def _refresh_analyst_market_fields(out: dict[str, Any], symbol: str) -> dict[str, Any]:
    out = dict(out)
    try:
        spot_payload = spot(symbol)
        price = _num_or_none(spot_payload.get("price"))
        if price is not None and price > 0:
            out["current_price"] = round(price, 3)
            out["current_price_source"] = spot_payload.get("source")
            out["current_price_as_of"] = spot_payload.get("as_of")
    except Exception as e:
        logger.warning("analyst spot refresh failed symbol=%s error=%s: %s", symbol, type(e).__name__, e)

    source = str(out.get("target_price_source") or "")
    has_explicit_target = bool(source and not source.startswith("model_"))
    if not has_explicit_target:
        out["implied_target"] = None
        out["target_price_source"] = None
        out["target_price_method"] = None
        out["target_price_confidence"] = None
        out["target_horizon_days"] = None
        current_price = _num_or_none(out.get("current_price"))
        if current_price is not None and current_price > 0:
            try:
                start = (date.today() - timedelta(days=130)).strftime("%Y%m%d")
                end = date.today().strftime("%Y%m%d")
                rows = klines(symbol=symbol, start=start, end=end, adjust="qfq")
                target = _technical_model_target(rows, current_price)
                if target is not None:
                    _mark_source("model_target", True)
                    out["implied_target"] = target["target"]
                    out["target_price_source"] = MODEL_TARGET_SOURCE
                    out["target_price_method"] = MODEL_TARGET_METHOD
                    out["target_price_confidence"] = target["confidence"]
                    out["target_horizon_days"] = 30
                else:
                    _mark_source("model_target", True, "no constructive setup")
            except Exception as e:
                logger.warning("model_target calc failed symbol=%s error=%s: %s", symbol, type(e).__name__, e)
                _mark_source("model_target", False, "calculation error")

    return _sanitize_analyst_payload(out)


# Cache the stock_basic / hk_basic name lookups once per process startup.
_NAME_CACHE: dict[str, str] = {}


def _resolve_name(ts_code: str, market: str) -> str | None:
    if ts_code in _NAME_CACHE:
        return _NAME_CACHE[ts_code] or None
    try:
        if market == "hk":
            df = _pro.hk_basic(fields="ts_code,name")
        else:
            df = _pro.stock_basic(list_status="L", fields="ts_code,name")
    except Exception as e:
        logger.warning("resolve_name failed ts_code=%s market=%s error=%s: %s", ts_code, market, type(e).__name__, e)
        return None
    if df is None or df.empty:
        return None
    for r in df.itertuples():
        _NAME_CACHE[r.ts_code] = r.name
    # Negative-cache codes absent from the listing (delisted, bogus), else
    # every lookup for them re-downloads the entire multi-thousand-row table.
    _NAME_CACHE.setdefault(ts_code, "")
    return _NAME_CACHE.get(ts_code) or None


# ---------- endpoints ------------------------------------------------------


@app.get("/health")
def health():
    sources = _source_status_snapshot()
    logger.info("health check sources=%s", {k: v.get("ok") for k, v in sources.items()})
    return {
        "ok": True,
        "time": datetime.now().isoformat(),
        "db_path": str(DB_PATH),
        "spot_priority": [
            "easy_tdx",
            "eastmoney_push2",
            "tencent_quote",
            "sina_quote",
            "tushare_daily",
        ],
        "kline_priority": ["easy_tdx", "tushare_pro_bar"],
        "minute_kline": {
            "source": "tushare_stk_mins",
            "realtime": False,
            "frequencies": list(_MINUTE_FREQS),
        },
        "sources": sources,
    }


@app.get("/klines", response_model=list[Kline])
def klines(
    symbol: str = Query(..., description="e.g. sh600519, 000858, hk00700"),
    start: str = Query("20230101"),
    end: str | None = Query(None),
    adjust: str = Query("qfq", pattern="^(|qfq|hfq)$"),
):
    end = end or date.today().strftime("%Y%m%d")
    start, end = _checked_date(start, "start"), _checked_date(end, "end")
    if start > end:
        raise HTTPException(400, f"start {start} is after end {end}")
    key = f"kline:{_norm_symbol(symbol)}:{start}:{end}:{adjust}"
    cached = cache_get(key)
    if cached is not None:
        return cached

    ts_code, market = _to_ts_code(symbol)

    # easy-tdx first for A-shares (no token/quota); Tushare is the fallback.
    if market in {"sh", "sz", "bj"}:
        tdx_rows = _tdx_klines(market, ts_code, start, end, adjust)
        if tdx_rows is not None:
            cache_put(key, tdx_rows, seconds_until_next_trading_close())
            return tdx_rows

    try:
        if market == "hk":
            # akshare for HK — Tushare's hk_daily is capped at 10/day.
            ak_code = ts_code.split(".")[0]  # "00700"
            df = _with_retries(
                ak.stock_hk_hist,
                symbol=ak_code, period="daily",
                start_date=start, end_date=end, adjust=(adjust or ""),
            )
            _mark_source("akshare_hk_hist", True)
        else:
            df = _with_retries(
                ts.pro_bar,
                ts_code=ts_code, api=_pro, adj=(adjust or None), start_date=start, end_date=end,
            )
            _mark_source("tushare_pro_bar", True)
    except Exception as e:
        if market == "hk":
            _mark_source("akshare_hk_hist", False, str(e))
        else:
            _mark_source("tushare_pro_bar", False, str(e))
        raise HTTPException(502, f"upstream error: {e}") from e

    if df is None or df.empty:
        _mark_source("akshare_hk_hist" if market == "hk" else "tushare_pro_bar", False, "empty response")
        logger.warning("klines empty upstream symbol=%s start=%s end=%s market=%s", symbol, start, end, market)
        cache_put(key, [], 3600)
        return []

    if market == "hk":
        # akshare HK schema: 日期 / 开盘 / 最高 / 最低 / 收盘 / 成交量 ...
        df = df.rename(columns={
            "日期": "date", "开盘": "open", "最高": "high",
            "最低": "low", "收盘": "close", "成交量": "volume",
        })
        df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
        rows = df[["date", "open", "high", "low", "close", "volume"]].to_dict(orient="records")
    else:
        df = df.sort_values("trade_date")
        rows = [
            {
                "date": f"{d[:4]}-{d[4:6]}-{d[6:]}",
                "open": float(r.open),
                "high": float(r.high),
                "low": float(r.low),
                "close": float(r.close),
                "volume": float(r.vol),
                # Tushare daily amount is denominated in thousand yuan.
                "amount": float(r.amount) * 1000,
            }
            for r in df.itertuples()
            for d in [str(r.trade_date)]
        ]
    cache_put(key, rows, seconds_until_next_trading_close())
    return rows


@app.get("/minute-klines", response_model=MinuteKlineSeries)
def minute_klines(
    symbol: str = Query(..., description="A-share symbol, e.g. sh600519 or 000858"),
    start: str = Query(
        ...,
        description="YYYYMMDD, YYYY-MM-DD, or YYYY-MM-DD HH:MM:SS",
    ),
    end: str | None = Query(
        None,
        description="Defaults to 15:00:00 on the start date",
    ),
    freq: str = Query("1min", pattern="^(1|5|15|30|60)min$"),
):
    start_dt, end_dt = _checked_minute_range(start, end)
    ts_code, market = _to_ts_code(symbol)
    if market not in {"sh", "sz", "bj"}:
        raise HTTPException(400, "historical minute klines currently support A-shares only")

    start_value = start_dt.strftime("%Y-%m-%d %H:%M:%S")
    end_value = end_dt.strftime("%Y-%m-%d %H:%M:%S")
    normalized_symbol = _norm_symbol(symbol)
    key = f"minute-kline:v1:{normalized_symbol}:{start_value}:{end_value}:{freq}"
    cached = cache_get(key)
    if cached is not None:
        return cached

    try:
        df = _with_retries(
            _pro.stk_mins,
            ts_code=ts_code,
            freq=freq,
            start_date=start_value,
            end_date=end_value,
            fields="ts_code,trade_time,open,high,low,close,vol,amount",
        )
        _mark_source("tushare_stk_mins", True)
    except Exception as e:
        _mark_source("tushare_stk_mins", False, str(e))
        raise HTTPException(502, f"tushare historical minute error: {e}") from e

    bars: list[dict[str, Any]] = []
    if df is not None and not df.empty:
        df = df.sort_values("trade_time")
        bars = [
            {
                "time": str(r.trade_time),
                "open": float(r.open),
                "high": float(r.high),
                "low": float(r.low),
                "close": float(r.close),
                "volume": float(r.vol),
                "amount": float(r.amount),
            }
            for r in df.itertuples()
        ]
    else:
        _mark_source("tushare_stk_mins", True, "empty response")

    out = {
        "symbol": normalized_symbol,
        "ts_code": ts_code,
        "freq": freq,
        "source": "tushare_stk_mins",
        # stk_mins is the historical-minute product. It must not be used as
        # an intraday realtime quote even if a provider later exposes same-day rows.
        "realtime": False,
        "bars": bars,
    }
    ttl = 30 * 24 * 3600 if end_dt.date() < date.today() else 300
    cache_put(key, out, ttl)
    return out


@app.get("/fundamental", response_model=Fundamental)
def fundamental(symbol: str):
    key = f"fund:v2:{_norm_symbol(symbol)}"
    cached = cache_get(key)
    if cached is not None:
        return cached

    ts_code, market = _to_ts_code(symbol)
    out: dict[str, Any] = {"symbol": symbol, "name": _resolve_name(ts_code, market)}

    ak_spot = _ak_a_spot(ts_code, market)
    if ak_spot is not None:
        out["name"] = str(ak_spot.get("名称") or out.get("name") or "")
        pe_ttm = _num_or_none(_ak_col(pd.Series(ak_spot), "市盈率-TTM", "市盈率-动态", "市盈率", "PE"))
        pb = _num_or_none(_ak_col(pd.Series(ak_spot), "市净率", "PB"))
        market_cap = _market_cap_to_yi(_num_or_none(_ak_col(pd.Series(ak_spot), "总市值")))
        if pe_ttm is not None:
            out["pe_ttm"] = pe_ttm
        if pb is not None:
            out["pb"] = pb
        if market_cap is not None:
            out["market_cap"] = market_cap
        _attach_profit_yoy(out, ts_code, market)
        if out.get("pe_ttm") is not None and out.get("pb") is not None and out.get("market_cap") is not None:
            cache_put(key, out, 24 * 3600 if out.get("profit_yoy") is not None else 30)
            return out

    try:
        if market == "hk":
            # daily_basic is A-share only; for HK we leave fundamentals blank.
            cache_put(key, out, 24 * 3600)
            return out
        # Latest trading day's basic metrics. Pull last 5 days then take tail.
        today = date.today().strftime("%Y%m%d")
        start = (date.today() - timedelta(days=10)).strftime("%Y%m%d")
        df = _with_retries(
            _pro.daily_basic,
            ts_code=ts_code, start_date=start, end_date=today,
            fields="ts_code,trade_date,close,pe_ttm,pb,total_mv",
        )
    except Exception as e:
        logger.error("fundamental upstream failed symbol=%s market=%s error=%s: %s", symbol, market, type(e).__name__, e)
        raise HTTPException(502, f"tushare error: {e}") from e

    if df is not None and not df.empty:
        latest = df.sort_values("trade_date").iloc[-1]
        if pd.notna(latest.get("pe_ttm")):
            out["pe_ttm"] = float(latest["pe_ttm"])
        if pd.notna(latest.get("pb")):
            out["pb"] = float(latest["pb"])
        if pd.notna(latest.get("total_mv")):
            # tushare returns 万元 -> convert to 亿元
            out["market_cap"] = float(latest["total_mv"]) / 1e4
        _attach_profit_yoy(out, ts_code, market)

    cache_put(key, out, 24 * 3600)
    return out


@app.get("/analyst", response_model=Analyst)
def analyst(symbol: str):
    """Realtime price plus a rules-based target.

    This endpoint deliberately skips broker-report/news/LLM aggregation. The
    target is a short-horizon trading target derived from auditable market data
    (current quote, qfq daily bars, ATR, momentum and recent resistance). It is
    not a sell-side target and never uses EPS * PE.
    """
    return _refresh_analyst_market_fields({"symbol": symbol}, symbol)


@app.get("/analysts", response_model=list[Analyst])
def analysts(symbols: str = Query(..., description="comma-separated symbols")):
    uniq = [s.strip() for s in symbols.split(",") if s.strip()]
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for symbol in uniq:
        if symbol in seen:
            continue
        seen.add(symbol)
        try:
            out.append(analyst(symbol))
        except Exception as e:
            # Keep a batch refresh useful even if one upstream symbol fails.
            logger.warning("analysts batch failed symbol=%s error=%s: %s", symbol, type(e).__name__, e)
            out.append({"symbol": symbol})
    return out


@app.get("/spot")
def spot(symbol: str):
    """Most-recent close (Tushare Pro has no realtime quote). 30s cache."""
    key = f"spot:{_norm_symbol(symbol)}"
    cached = cache_get(key)
    if cached is not None:
        return cached

    ts_code, market = _to_ts_code(symbol)
    start = (date.today() - timedelta(days=10)).strftime("%Y%m%d")
    end = date.today().strftime("%Y%m%d")
    try:
        if market in {"sh", "sz", "bj"}:
            tdx_spot = _tdx_spot(symbol, ts_code, market)
            if tdx_spot is not None:
                cache_put(key, tdx_spot, 30)
                return tdx_spot
            ak_spot = _ak_a_spot(ts_code, market)
            price = _spot_price_from_ak(ak_spot) if ak_spot is not None else None
            if ak_spot is not None and price is not None:
                out = {
                    "symbol": symbol,
                    "name": str(ak_spot.get("名称") or _resolve_name(ts_code, market) or ""),
                    "price": price,
                    "change_pct": _spot_change_pct_from_ak(ak_spot) or 0,
                    "volume": _num_or_none(ak_spot.get("成交量")) or 0,
                    "turnover": _num_or_none(ak_spot.get("成交额")) or 0,
                    "source": "eastmoney_push2",
                    "as_of": datetime.now().isoformat(),
                }
                cache_put(key, out, 30)
                return out
            tencent_spot = _tencent_spot(symbol, ts_code, market)
            if tencent_spot is not None:
                cache_put(key, tencent_spot, 30)
                return tencent_spot
            sina_spot = _sina_spot(symbol, ts_code, market)
            if sina_spot is not None:
                cache_put(key, sina_spot, 30)
                return sina_spot
        if market == "hk":
            ak_code = ts_code.split(".")[0]
            df = _with_retries(
                ak.stock_hk_hist,
                symbol=ak_code, period="daily", start_date=start, end_date=end, adjust="",
            )
            _mark_source("akshare_hk_hist", True)
            if df is None or df.empty:
                _mark_source("akshare_hk_hist", False, "empty response")
                raise HTTPException(404, f"symbol {symbol} not found")
            df = df.rename(columns={
                "日期": "trade_date", "开盘": "open", "最高": "high",
                "最低": "low", "收盘": "close", "成交量": "vol",
                "成交额": "amount", "涨跌幅": "pct_chg",
            })
            # Take the latest bar deterministically instead of trusting
            # upstream row order (A-share path below already sorts).
            df = df.sort_values("trade_date")
        else:
            # A-share fallback when the AkShare/Eastmoney realtime quote is
            # unavailable or too slow.
            df = _with_retries(_pro.daily, ts_code=ts_code, start_date=start, end_date=end)
            _mark_source("tushare_daily", True)
            if df is None or df.empty:
                _mark_source("tushare_daily", False, "empty response")
                raise HTTPException(404, f"symbol {symbol} not found")
            df = df.sort_values("trade_date")
    except HTTPException:
        raise
    except Exception as e:
        if market == "hk":
            _mark_source("akshare_hk_hist", False, str(e))
        else:
            _mark_source("tushare_daily", False, str(e))
        raise HTTPException(502, f"upstream error: {e}") from e
    r = df.iloc[-1]
    out = {
        "symbol": symbol,
        "name": _resolve_name(ts_code, market) or "",
        "price": float(r.get("close", 0) or 0),
        "change_pct": float(r.get("pct_chg", 0) or 0),
        "volume": float(r.get("vol", 0) or 0),
        "turnover": float(r.get("amount", 0) or 0),
        "source": "akshare_hk_hist" if market == "hk" else "tushare_daily",
        "as_of": f"{str(r.get('trade_date'))[:4]}-{str(r.get('trade_date'))[4:6]}-{str(r.get('trade_date'))[6:]}"
        if market != "hk" and r.get("trade_date") is not None else datetime.now().isoformat(),
    }
    cache_put(key, out, 30)
    return out


@app.get("/spots")
def spots(symbols: str = Query(..., description="comma-separated symbols")):
    """Batch spot quotes for the frontend table.

    This endpoint keeps caching authoritative in pyserver while avoiding the
    Next.js layer fanning one browser batch out into dozens of HTTP requests.
    """
    uniq: list[str] = []
    seen: set[str] = set()
    for raw in symbols.split(","):
        symbol = raw.strip()
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        uniq.append(symbol)

    out: list[dict[str, Any]] = []
    missing: list[str] = []
    for symbol in uniq:
        cached = cache_get(f"spot:{_norm_symbol(symbol)}")
        if cached is not None:
            out.append(cached)
        else:
            missing.append(symbol)

    if missing:
        with ThreadPoolExecutor(max_workers=min(_SPOT_BATCH_CONCURRENCY, len(missing))) as executor:
            futures = {executor.submit(spot, symbol): symbol for symbol in missing}
            for future in as_completed(futures):
                try:
                    out.append(future.result())
                except Exception as e:
                    # Keep a batch refresh useful even if one upstream symbol fails.
                    failed_symbol = futures[future]
                    logger.warning("spots batch failed symbol=%s error=%s: %s", failed_symbol, type(e).__name__, e)
                    continue

    by_symbol = {str(row.get("symbol")): row for row in out}
    return [by_symbol[symbol] for symbol in uniq if symbol in by_symbol]


# ---------- moneyflow (资金流向) endpoint ------------------------------------

class MoneyflowRow(BaseModel):
    trade_date: str
    buy_lg_amount: float | None = None   # 大单买入(万)
    sell_lg_amount: float | None = None  # 大单卖出(万)
    buy_elg_amount: float | None = None  # 特大单买入(万)
    sell_elg_amount: float | None = None # 特大单卖出(万)
    net_mf_amount: float | None = None   # 净流入(万)


class MoneyflowResponse(BaseModel):
    symbol: str
    rows: list[MoneyflowRow]


_MONEYFLOW_LIMITER = _TokenBucket(n=15, window_s=60)


def _checked_daily_range(
    days: int,
    start_date: str | None,
    end_date: str | None,
    *,
    max_calendar_days: int = 2000,
) -> tuple[str, str, bool]:
    """Resolve an explicit point-in-time date range or a legacy rolling window."""
    explicit = start_date is not None or end_date is not None
    end_value = end_date or date.today().strftime("%Y%m%d")
    try:
        end_dt = datetime.strptime(end_value, "%Y%m%d").date()
        start_dt = (
            datetime.strptime(start_date, "%Y%m%d").date()
            if start_date
            else end_dt - timedelta(days=days * 2 + 10)
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="dates must use YYYYMMDD") from exc
    if start_dt > end_dt:
        raise HTTPException(status_code=400, detail="start_date must not exceed end_date")
    if (end_dt - start_dt).days > max_calendar_days:
        raise HTTPException(status_code=400, detail=f"date range exceeds {max_calendar_days} calendar days")
    return start_dt.strftime("%Y%m%d"), end_dt.strftime("%Y%m%d"), explicit


@app.get("/moneyflow", response_model=MoneyflowResponse)
def moneyflow(
    symbol: str = Query(..., description="e.g. sz000001 or sh600519"),
    days: int = Query(20, ge=1, le=500, description="legacy lookback trading days"),
    start_date: str | None = Query(None, pattern=r"^\d{8}$"),
    end_date: str | None = Query(None, pattern=r"^\d{8}$"),
):
    """Individual stock money-flow (资金流向) from Tushare moneyflow API.

    Requires Tushare 5000+ points. Returns large/extra-large order flow
    for the Tide strategy's capital-flow signals.
    """
    ts_code, _market = _to_ts_code(symbol)
    start, end, explicit_range = _checked_daily_range(days, start_date, end_date)
    cache_key = f"moneyflow:{ts_code}:{start}:{end}"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached

    _MONEYFLOW_LIMITER.acquire()
    try:
        df = _with_retries(
            _pro.moneyflow,
            ts_code=ts_code,
            start_date=start,
            end_date=end,
        )
    except Exception as e:
        logger.warning("moneyflow failed symbol=%s: %s", symbol, e)
        raise HTTPException(status_code=502, detail=f"moneyflow upstream error: {e}")

    if df is None or df.empty:
        return MoneyflowResponse(symbol=symbol, rows=[])

    df = df.sort_values("trade_date")
    if not explicit_range:
        df = df.tail(days)
    rows = [
        MoneyflowRow(
            trade_date=str(r["trade_date"]),
            buy_lg_amount=_safe_float(r.get("buy_lg_amount")),
            sell_lg_amount=_safe_float(r.get("sell_lg_amount")),
            buy_elg_amount=_safe_float(r.get("buy_elg_amount")),
            sell_elg_amount=_safe_float(r.get("sell_elg_amount")),
            net_mf_amount=_safe_float(r.get("net_mf_amount")),
        )
        for _, r in df.iterrows()
    ]
    result = MoneyflowResponse(symbol=symbol, rows=rows)
    cache_put(cache_key, result.model_dump(), 3600)
    return result


def _safe_float(v: Any) -> float | None:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ---------- top_list (龙虎榜) endpoint ----------------------------------------

class TopListRow(BaseModel):
    trade_date: str
    reason: str | None = None          # 上榜原因
    buy: float | None = None           # 买入额(万)
    sell: float | None = None          # 卖出额(万)
    net_buy: float | None = None       # 净买入(万)


class TopListResponse(BaseModel):
    symbol: str
    rows: list[TopListRow]


_TOPLIST_LIMITER = _TokenBucket(n=15, window_s=60)


@app.get("/top-list", response_model=TopListResponse)
def top_list(
    symbol: str = Query(..., description="e.g. sz000001 or sh600519"),
    days: int = Query(30, ge=1, le=90, description="lookback calendar days"),
):
    """Dragon-Tiger list (龙虎榜) from Tushare top_list API.

    Shows institutional/brokerage block trades. Useful for detecting
    smart-money activity in the Tide strategy.
    """
    ts_code, _market = _to_ts_code(symbol)
    cache_key = f"toplist:{ts_code}:{days}"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached

    end = date.today().strftime("%Y%m%d")
    start = (date.today() - timedelta(days=days)).strftime("%Y%m%d")
    _TOPLIST_LIMITER.acquire()
    try:
        df = _with_retries(
            _pro.top_list,
            ts_code=ts_code,
            start_date=start,
            end_date=end,
        )
    except Exception as e:
        logger.warning("top_list failed symbol=%s: %s", symbol, e)
        raise HTTPException(status_code=502, detail=f"top_list upstream error: {e}")

    if df is None or df.empty:
        return TopListResponse(symbol=symbol, rows=[])

    df = df.sort_values("trade_date").tail(days)
    rows = [
        TopListRow(
            trade_date=str(r["trade_date"]),
            reason=str(r.get("reason", "")) or None,
            buy=_safe_float(r.get("buy")),
            sell=_safe_float(r.get("sell")),
            net_buy=_safe_float(r.get("net_buy")),
        )
        for _, r in df.iterrows()
    ]
    result = TopListResponse(symbol=symbol, rows=rows)
    cache_put(cache_key, result.model_dump(), 3600)
    return result


# ---------- margin_detail (融资融券) endpoint ----------------------------------

class MarginRow(BaseModel):
    trade_date: str
    rzye: float | None = None          # 融资余额(元)
    rqye: float | None = None          # 融券余额(元)
    rzmre: float | None = None         # 融资买入额(元)
    rqchl: float | None = None         # 融券偿还量(股)


class MarginResponse(BaseModel):
    symbol: str
    rows: list[MarginRow]


_MARGIN_LIMITER = _TokenBucket(n=15, window_s=60)


@app.get("/margin-detail", response_model=MarginResponse)
def margin_detail(
    symbol: str = Query(..., description="e.g. sz000001 or sh600519"),
    days: int = Query(20, ge=1, le=500, description="legacy lookback trading days"),
    start_date: str | None = Query(None, pattern=r"^\d{8}$"),
    end_date: str | None = Query(None, pattern=r"^\d{8}$"),
):
    """Margin trading detail (融资融券) from Tushare margin_detail API.

    Rising margin balance (融资余额) indicates leveraged bullish sentiment.
    Useful as a confirmation signal for the Tide strategy.
    """
    ts_code, _market = _to_ts_code(symbol)
    start, end, explicit_range = _checked_daily_range(days, start_date, end_date)
    cache_key = f"margin:{ts_code}:{start}:{end}"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached

    _MARGIN_LIMITER.acquire()
    try:
        df = _with_retries(
            _pro.margin_detail,
            ts_code=ts_code,
            start_date=start,
            end_date=end,
        )
    except Exception as e:
        logger.warning("margin_detail failed symbol=%s: %s", symbol, e)
        raise HTTPException(status_code=502, detail=f"margin_detail upstream error: {e}")

    if df is None or df.empty:
        return MarginResponse(symbol=symbol, rows=[])

    df = df.sort_values("trade_date")
    if not explicit_range:
        df = df.tail(days)
    rows = [
        MarginRow(
            trade_date=str(r["trade_date"]),
            rzye=_safe_float(r.get("rzye")),
            rqye=_safe_float(r.get("rqye")),
            rzmre=_safe_float(r.get("rzmre")),
            rqchl=_safe_float(r.get("rqchl")),
        )
        for _, r in df.iterrows()
    ]
    result = MarginResponse(symbol=symbol, rows=rows)
    cache_put(cache_key, result.model_dump(), 3600)
    return result


# ---------- index-daily endpoint (Prism regime detection) --------------------

INDEX_CODES = {
    "hs300": "000300.SH",   # 沪深300
    "zz1000": "000852.SH",  # 中证1000
    "cyb": "399006.SZ",     # 创业板指
    "sz50": "000016.SH",    # 上证50
}


class IndexDailyRow(BaseModel):
    trade_date: str
    open: float | None = None
    close: float | None = None
    high: float | None = None
    low: float | None = None
    pct_chg: float | None = None   # daily % change
    vol: float | None = None       # volume (手)


class IndexDailyResponse(BaseModel):
    index_code: str
    index_name: str
    rows: list[IndexDailyRow]


@app.get("/index-daily", response_model=IndexDailyResponse)
def index_daily(
    index: str = Query("hs300", description="hs300 / zz1000 / cyb / sz50"),
    days: int = Query(60, ge=1, le=1000, description="legacy lookback trading days"),
    start_date: str | None = Query(None, pattern=r"^\d{8}$"),
    end_date: str | None = Query(None, pattern=r"^\d{8}$"),
):
    """Index daily klines from Tushare index_daily API.

    Used by the Prism strategy for market regime detection.
    """
    ts_code = INDEX_CODES.get(index, index if "." in index else "000300.SH")
    start, end, explicit_range = _checked_daily_range(days, start_date, end_date)
    cache_key = f"index_daily:{ts_code}:{start}:{end}"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached

    try:
        df = _with_retries(
            _pro.index_daily,
            ts_code=ts_code,
            start_date=start,
            end_date=end,
        )
    except Exception as e:
        logger.warning("index_daily failed ts_code=%s: %s", ts_code, e)
        raise HTTPException(status_code=502, detail=f"index_daily upstream error: {e}")

    if df is None or df.empty:
        return IndexDailyResponse(index_code=ts_code, index_name=index, rows=[])

    df = df.sort_values("trade_date")
    if not explicit_range:
        df = df.tail(days)
    rows = [
        IndexDailyRow(
            trade_date=str(r["trade_date"]),
            open=_safe_float(r.get("open")),
            close=_safe_float(r.get("close")),
            high=_safe_float(r.get("high")),
            low=_safe_float(r.get("low")),
            pct_chg=_safe_float(r.get("pct_chg")),
            vol=_safe_float(r.get("vol")),
        )
        for _, r in df.iterrows()
    ]
    result = IndexDailyResponse(index_code=ts_code, index_name=index, rows=rows)
    cache_put(cache_key, result.model_dump(), 3600)
    return result


# ---------- market-breadth endpoint (Prism regime detection) -----------------

class MarketBreadthResponse(BaseModel):
    trade_date: str
    advance_count: int          # 上涨家数
    decline_count: int          # 下跌家数
    flat_count: int             # 平盘家数
    advance_ratio: float        # 上涨比例
    new_high_20: int            # 20日新高家数
    new_low_20: int             # 20日新低家数
    total_amount: float | None  # 全市场成交额(万元)
    limit_up_count: int         # 涨停家数
    limit_down_count: int       # 跌停家数


@app.get("/market-breadth", response_model=MarketBreadthResponse)
def market_breadth():
    """Market-wide breadth metrics for regime detection.

    Uses Tushare daily_basic + stk_limit to compute advance/decline,
    new highs/lows, and limit-up/down counts across the full A-share market.
    """
    cache_key = "market_breadth:latest"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached

    today_str = date.today().strftime("%Y%m%d")
    yesterday = (date.today() - timedelta(days=1)).strftime("%Y%m%d")

    # Get today's daily data for all stocks
    try:
        df_today = _with_retries(
            _pro.daily,
            trade_date=today_str,
        )
    except Exception:
        # Market might not be open yet today; try yesterday
        try:
            df_today = _with_retries(_pro.daily, trade_date=yesterday)
            today_str = yesterday
        except Exception as e:
            logger.warning("market_breadth daily failed: %s", e)
            raise HTTPException(status_code=502, detail=f"market_breadth upstream error: {e}")

    if df_today is None or df_today.empty:
        return MarketBreadthResponse(
            trade_date=today_str,
            advance_count=0, decline_count=0, flat_count=0,
            advance_ratio=0.0, new_high_20=0, new_low_20=0,
            total_amount=None, limit_up_count=0, limit_down_count=0,
        )

    # Advance/decline
    pct = df_today["pct_chg"].fillna(0)
    advance_count = int((pct > 0).sum())
    decline_count = int((pct < 0).sum())
    flat_count = int((pct == 0).sum())
    total = advance_count + decline_count + flat_count
    advance_ratio = advance_count / max(total, 1)

    # Total market turnover
    total_amount = _safe_float(df_today["amount"].sum())

    # Limit up/down from stk_limit
    try:
        df_limit = _with_retries(_pro.stk_limit, trade_date=today_str)
        limit_up_count = 0
        limit_down_count = 0
        if df_limit is not None and not df_limit.empty:
            # A stock is at limit up if close >= up_limit (with small tolerance)
            merged = df_today[["ts_code", "close"]].merge(
                df_limit[["ts_code", "up_limit", "down_limit"]], on="ts_code", how="inner"
            )
            limit_up_count = int((merged["close"] >= merged["up_limit"] * 0.999).sum())
            limit_down_count = int((merged["close"] <= merged["down_limit"] * 1.001).sum())
    except Exception as e:
        logger.warning("market_breadth stk_limit failed: %s", e)
        limit_up_count = 0
        limit_down_count = 0

    # New 20-day highs/lows — requires historical data, skip if too slow
    # For now, approximate using pct_chg extremes
    new_high_20 = int((pct >= 9.9).sum())  # proxy: stocks up ~10%+
    new_low_20 = int((pct <= -9.9).sum())  # proxy: stocks down ~10%+

    result = MarketBreadthResponse(
        trade_date=today_str,
        advance_count=advance_count,
        decline_count=decline_count,
        flat_count=flat_count,
        advance_ratio=round(advance_ratio, 4),
        new_high_20=new_high_20,
        new_low_20=new_low_20,
        total_amount=total_amount,
        limit_up_count=limit_up_count,
        limit_down_count=limit_down_count,
    )
    cache_put(cache_key, result.model_dump(), 300)  # 5 min cache
    return result


# ---------- index-weight endpoint (指数成分权重) ------------------------------

class IndexWeightRow(BaseModel):
    ts_code: str              # constituent stock code
    trade_date: str
    weight: float | None = None   # weight in index (percentage)


class IndexWeightResponse(BaseModel):
    index_code: str
    trade_date: str
    rows: list[IndexWeightRow]


_INDEX_WEIGHT_LIMITER = _TokenBucket(n=10, window_s=60)


@app.get("/index-weight", response_model=IndexWeightResponse)
def index_weight(
    index: str = Query("hs300", description="hs300 / zz1000 / cyb / sz50"),
):
    """Index constituent weights from Tushare index_weight API.

    Returns the latest available weight snapshot for the given index.
    Useful for computing industry diffusion and sector concentration.
    """
    ts_index = INDEX_CODES.get(index, index if "." in index else f"{index}.SH")
    cache_key = f"index_weight:{ts_index}"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached

    # Get the most recent trade date available
    today_str = date.today().strftime("%Y%m%d")
    start = (date.today() - timedelta(days=30)).strftime("%Y%m%d")

    _INDEX_WEIGHT_LIMITER.acquire()
    try:
        df = _with_retries(
            _pro.index_weight,
            ts_code=ts_index,
            start_date=start,
            end_date=today_str,
        )
    except Exception as e:
        logger.warning("index_weight failed index=%s: %s", index, e)
        raise HTTPException(status_code=502, detail=f"index_weight upstream error: {e}")

    if df is None or df.empty:
        return IndexWeightResponse(index_code=ts_index, trade_date=today_str, rows=[])

    # Keep only the latest trade_date snapshot
    latest_date = str(df["trade_date"].max())
    df_latest = df[df["trade_date"] == latest_date].sort_values("weight", ascending=False)

    rows = [
        IndexWeightRow(
            ts_code=str(r["ts_code"]),
            trade_date=str(r["trade_date"]),
            weight=_safe_float(r.get("weight")),
        )
        for _, r in df_latest.iterrows()
    ]
    result = IndexWeightResponse(index_code=ts_index, trade_date=latest_date, rows=rows)
    cache_put(cache_key, result.model_dump(), 86400)  # 24h cache
    return result
