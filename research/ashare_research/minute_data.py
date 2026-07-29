"""Historical minute-bar data warehouse: probe, sync, and coverage reporting.

Storage layout (Zstandard Parquet)::

    research/runtime/minute/
      raw/freq=5min/ts_code=000001.SZ/year=2025/month=01/part.parquet
      meta/coverage.json
      meta/last-sync.json
      meta/last-sync-error.json

Unique key: (ts_code, trade_time, freq).
Incremental writes merge, deduplicate, sort by trade_time ascending, then
atomically replace via temp file + os.replace.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl

from .data_sync import build_tushare_client

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VALID_FREQS = ("1min", "5min", "15min", "30min", "60min")

_EXPECTED_BARS_PER_DAY: dict[str, int] = {
    "1min": 240,
    "5min": 48,
    "15min": 16,
    "30min": 8,
    "60min": 4,
}

# Maximum natural days per request segment for 1min data.
_1MIN_MAX_DAYS = 31

# For 5min+, each segment must keep theoretical max rows < 8000.
# 5min: 48 bars/day => 8000/48 ≈ 166 days safe; use 120 to be conservative.
_FREQ_MAX_DAYS: dict[str, int] = {
    "1min": 31,
    "5min": 120,
    "15min": 365,
    "30min": 365,
    "60min": 365,
}

# If a response has exactly this many rows, treat as possibly truncated.
_TRUNCATION_THRESHOLD = 8000

# A-share trading sessions (bar close timestamps for 5min).
_AM_START = "09:35:00"
_AM_END = "11:30:00"
_PM_START = "13:05:00"
_PM_END = "15:00:00"

_MINUTE_COLUMNS = (
    "ts_code",
    "symbol",
    "trade_date",
    "trade_time",
    "freq",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
    "source",
    "realtime",
    "fetched_at",
)

_MINUTE_SCHEMA: dict[str, Any] = {
    "ts_code": pl.String,
    "symbol": pl.String,
    "trade_date": pl.String,
    "trade_time": pl.String,
    "freq": pl.String,
    "open": pl.Float64,
    "high": pl.Float64,
    "low": pl.Float64,
    "close": pl.Float64,
    "volume": pl.Float64,
    "amount": pl.Float64,
    "source": pl.String,
    "realtime": pl.Boolean,
    "fetched_at": pl.String,
}


# ---------------------------------------------------------------------------
# Data contracts
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProbeResult:
    ts_code: str
    freq: str
    date: str
    rows: int
    first_time: str | None
    last_time: str | None
    passed: bool
    note: str = ""


@dataclass
class MinuteSyncReport:
    started_at: str
    completed_at: str = ""
    freq: str = "5min"
    start_date: str = ""
    end_date: str = ""
    symbols_requested: int = 0
    symbols_synced: int = 0
    total_rows: int = 0
    written_partitions: int = 0
    skipped_partitions: int = 0
    failures: list[dict[str, str]] = field(default_factory=list)
    passed: bool = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _atomic_parquet(frame: pl.DataFrame, target: Path) -> None:
    """Write parquet atomically via temp file + os.replace."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=target.name, suffix=".tmp", dir=target.parent)
    os.close(fd)
    try:
        frame.write_parquet(tmp, compression="zstd")
        os.replace(tmp, target)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _redact_error(error: Any) -> str:
    text = str(error)
    token = os.environ.get("TUSHARE_TOKEN")
    if token:
        text = text.replace(token, "[REDACTED]")
    return text


def _symbol_to_ts_code(symbol: str) -> str:
    """Convert canonical, bare, or suffixed symbols to Tushare ``ts_code``."""
    symbol = symbol.strip()
    if "." in symbol:
        return symbol.upper()
    lowered = symbol.lower()
    for prefix, exchange in (("sh", "SH"), ("sz", "SZ"), ("bj", "BJ")):
        if lowered.startswith(prefix) and lowered[len(prefix) :].isdigit():
            return f"{lowered[len(prefix):]}.{exchange}"
    if symbol.startswith("6"):
        return f"{symbol}.SH"
    if symbol.startswith(("0", "3")):
        return f"{symbol}.SZ"
    if symbol.startswith(("4", "8")):
        return f"{symbol}.BJ"
    return f"{symbol}.SZ"


def _ts_code_to_symbol(ts_code: str) -> str:
    return ts_code.split(".")[0]


def _partition_path(root: Path, freq: str, ts_code: str, year: int, month: int) -> Path:
    return (
        root
        / "raw"
        / f"freq={freq}"
        / f"ts_code={ts_code}"
        / f"year={year}"
        / f"month={month:02d}"
        / "part.parquet"
    )


def _segment_date_ranges(
    start: date, end: date, freq: str
) -> list[tuple[date, date]]:
    """Split [start, end] into segments respecting per-freq max days."""
    max_days = _FREQ_MAX_DAYS.get(freq, 120)
    segments: list[tuple[date, date]] = []
    current = start
    while current <= end:
        seg_end = min(current + timedelta(days=max_days - 1), end)
        segments.append((current, seg_end))
        current = seg_end + timedelta(days=1)
    return segments


def _query_stk_mins_with_retry(
    pro: Any,
    ts_code: str,
    freq: str,
    start_dt: str,
    end_dt: str,
    attempts: int = 3,
    base_delay: float = 2.0,
) -> Any:
    """Query stk_mins with exponential backoff retry."""
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            return pro.stk_mins(
                ts_code=ts_code,
                freq=freq,
                start_date=start_dt,
                end_date=end_dt,
                fields="ts_code,trade_time,open,high,low,close,vol,amount",
            )
        except Exception as error:
            last_error = error
            if attempt + 1 < attempts:
                time.sleep(base_delay * (2**attempt))
    raise RuntimeError(
        f"stk_mins failed for {ts_code} after {attempts} attempts: {last_error}"
    )


# ---------------------------------------------------------------------------
# Probe
# ---------------------------------------------------------------------------


def probe_minute_data(
    symbols: list[str],
    probe_date: str,
    freq: str = "5min",
    env_file: Path | None = None,
) -> list[ProbeResult]:
    """Probe minute data availability for given symbols and date."""
    pro = build_tushare_client(env_file)
    results: list[ProbeResult] = []
    for symbol in symbols:
        ts_code = _symbol_to_ts_code(symbol)
        start_dt = f"{probe_date} 09:30:00"
        end_dt = f"{probe_date} 15:00:00"
        try:
            df = _query_stk_mins_with_retry(pro, ts_code, freq, start_dt, end_dt)
            if df is not None and not df.empty:

                df = df.sort_values("trade_time")
                rows = len(df)
                first_time = str(df.iloc[0]["trade_time"])
                last_time = str(df.iloc[-1]["trade_time"])
                # Basic validation
                passed = rows > 0
                note = ""
                if rows == 0:
                    note = "empty response"
                    passed = False
                results.append(
                    ProbeResult(ts_code, freq, probe_date, rows, first_time, last_time, passed, note)
                )
            else:
                results.append(
                    ProbeResult(ts_code, freq, probe_date, 0, None, None, False, "empty response")
                )
        except Exception as e:
            results.append(
                ProbeResult(
                    ts_code,
                    freq,
                    probe_date,
                    0,
                    None,
                    None,
                    False,
                    _redact_error(e)[:200],
                )
            )
    return results


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------


def _normalize_minute_frame(
    frame: pl.DataFrame,
    *,
    require_complete: bool = False,
) -> pl.DataFrame:
    """Apply the canonical storage schema and discard provider-only columns."""
    if "vol" in frame.columns and "volume" not in frame.columns:
        frame = frame.rename({"vol": "volume"})
    elif "vol" in frame.columns:
        frame = frame.drop("vol")

    missing = sorted(set(_MINUTE_COLUMNS) - set(frame.columns))
    if require_complete and missing:
        raise ValueError(f"minute frame missing required columns: {missing}")

    expressions = [
        pl.col(column).cast(dtype, strict=False)
        for column, dtype in _MINUTE_SCHEMA.items()
        if column in frame.columns
    ]
    normalized = frame.with_columns(expressions)
    if "realtime" in normalized.columns:
        normalized = normalized.with_columns(
            pl.col("realtime").fill_null(False)
        )
    return normalized.select(
        [column for column in _MINUTE_COLUMNS if column in normalized.columns]
    )


def _fetch_symbol_segment(
    pro: Any,
    ts_code: str,
    freq: str,
    seg_start: date,
    seg_end: date,
    request_interval: float = 0.15,
) -> pl.DataFrame | None:
    """Fetch one segment for one symbol, handling truncation by splitting."""
    start_dt = f"{seg_start.isoformat()} 09:30:00"
    end_dt = f"{seg_end.isoformat()} 15:00:00"

    df = _query_stk_mins_with_retry(pro, ts_code, freq, start_dt, end_dt)
    if df is None or df.empty:
        return None

    import pandas as pd

    pdf = pd.DataFrame(df)
    rows = len(pdf)

    # Truncation detection: exactly 8000 rows means possibly truncated
    if rows >= _TRUNCATION_THRESHOLD and seg_start < seg_end:
        mid = seg_start + (seg_end - seg_start) / 2
        left = _fetch_symbol_segment(pro, ts_code, freq, seg_start, mid, request_interval)
        time.sleep(request_interval)
        right = _fetch_symbol_segment(
            pro, ts_code, freq, mid + timedelta(days=1), seg_end, request_interval
        )
        frames = [f for f in (left, right) if f is not None and f.height > 0]
        if frames:
            return pl.concat(frames)
        return None

    # Convert to Polars with standard schema
    pdf = pdf.sort_values("trade_time").reset_index(drop=True)
    pdf["trade_date"] = pdf["trade_time"].str[:10].str.replace("-", "")
    pdf["symbol"] = ts_code.split(".")[0]
    pdf["freq"] = freq
    pdf["source"] = "tushare_stk_mins"
    pdf["realtime"] = False
    pdf["fetched_at"] = datetime.now(UTC).isoformat()

    return _normalize_minute_frame(
        pl.from_pandas(pdf),
        require_complete=True,
    )


def _merge_partition(existing: Path, new_data: pl.DataFrame) -> pl.DataFrame:
    """Merge new data into existing partition, dedup by unique key."""
    new_data = _normalize_minute_frame(new_data, require_complete=True)
    if existing.exists() and existing.stat().st_size > 0:
        old = _normalize_minute_frame(pl.read_parquet(existing))
        combined = pl.concat([old, new_data], how="diagonal_relaxed")
    else:
        combined = new_data

    combined = _normalize_minute_frame(combined, require_complete=True)
    # Deduplicate on (ts_code, trade_time, freq), keeping last
    combined = combined.unique(subset=["ts_code", "trade_time", "freq"], keep="last")
    # Sort by trade_time ascending
    combined = combined.sort("trade_time")
    return combined


def sync_minute_data(
    root: Path | str,
    start_date: date,
    end_date: date,
    freq: str = "5min",
    universe_path: Path | str | None = None,
    symbols: list[str] | None = None,
    env_file: Path | None = None,
    refresh: bool = False,
    request_interval: float = 0.15,
    max_workers: int = 1,
    trading_dates: list[str] | None = None,
    expected_dates_by_symbol: dict[str, set[str]] | None = None,
    suspended_dates_by_symbol: dict[str, set[str]] | None = None,
) -> MinuteSyncReport:
    """Sync historical minute bars from Tushare stk_mins to partitioned Parquet.

    Args:
        root: Minute data root (e.g. research/runtime/minute).
        start_date: First date to sync.
        end_date: Last date to sync.
        freq: Bar frequency (1min/5min/15min/30min/60min).
        universe_path: Path to universe.json for symbol list.
        symbols: Explicit symbol list (overrides universe).
        env_file: Path to .env with TUSHARE_TOKEN.
        refresh: Force re-download even if partition exists.
        request_interval: Seconds between API calls.
        max_workers: Max concurrent workers (1 or 2).
        trading_dates: Known trading dates (YYYYMMDD) for completeness check.
        expected_dates_by_symbol: Per-symbol dates known to have daily volume.
        suspended_dates_by_symbol: Per-symbol suspension dates.
    """
    root = Path(root)
    if freq not in VALID_FREQS:
        raise ValueError(f"unsupported minute frequency: {freq}")
    if start_date > end_date:
        raise ValueError("start_date must be on or before end_date")
    if max_workers not in (1, 2):
        raise ValueError("max_workers must be 1 or 2")
    started = datetime.now(UTC)
    report = MinuteSyncReport(
        started_at=started.isoformat(),
        freq=freq,
        start_date=start_date.isoformat(),
        end_date=end_date.isoformat(),
    )

    # Resolve symbol list
    if symbols:
        ts_codes = [_symbol_to_ts_code(s) for s in symbols]
    elif universe_path:
        universe = json.loads(Path(universe_path).read_text("utf-8"))
        entries = universe.get("entries")
        if not isinstance(entries, list):
            raise ValueError("universe entries must be a list")
        interval_start = start_date.strftime("%Y%m%d")
        interval_end = end_date.strftime("%Y%m%d")
        ts_codes = []
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError("each universe entry must be an object")
            if entry.get("pool_tier", "core") not in ("core", "watch"):
                continue
            symbol = entry.get("symbol")
            if not isinstance(symbol, str) or len(symbol) != 6 or not symbol.isdigit():
                raise ValueError(f"invalid universe symbol: {symbol!r}")
            active_from = str(
                entry.get("strategy_from") or "0001-01-01"
            ).replace("-", "")
            active_until = str(
                entry.get("strategy_until") or "9999-12-31"
            ).replace("-", "")
            for label, value in (
                ("strategy_from", active_from),
                ("strategy_until", active_until),
            ):
                try:
                    datetime.strptime(value, "%Y%m%d")
                except ValueError as error:
                    raise ValueError(
                        f"invalid {label} for {symbol}: {value}"
                    ) from error
            if active_from > active_until:
                raise ValueError(
                    f"invalid strategy membership range for {symbol}"
                )
            if active_from > interval_end or active_until < interval_start:
                continue
            ts_codes.append(_symbol_to_ts_code(symbol))
    else:
        raise ValueError("either symbols or universe_path must be provided")
    ts_codes = list(dict.fromkeys(ts_codes))

    report.symbols_requested = len(ts_codes)
    segments = _segment_date_ranges(start_date, end_date, freq)
    synced_symbols = 0
    total_rows = 0
    written = 0
    skipped = 0

    def sync_symbol(
        ts_code: str,
        pro: Any,
    ) -> tuple[int, int, int, list[dict[str, str]]]:
        symbol_rows = 0
        symbol_written = 0
        symbol_skipped = 0
        symbol_failures: list[dict[str, str]] = []
        for seg_start, seg_end in segments:
            if not refresh:
                all_complete = bool(trading_dates)
                current = seg_start
                while all_complete and current <= seg_end:
                    month_prefix = f"{current.year:04d}{current.month:02d}"
                    segment_start = seg_start.strftime("%Y%m%d")
                    segment_end = seg_end.strftime("%Y%m%d")
                    symbol_expected_dates = (
                        expected_dates_by_symbol.get(ts_code)
                        if expected_dates_by_symbol is not None
                        else None
                    )
                    if symbol_expected_dates:
                        expected_dates = {
                            value
                            for value in symbol_expected_dates
                            if value.startswith(month_prefix)
                            and segment_start <= value <= segment_end
                        }
                    else:
                        expected_dates = {
                            value
                            for value in (trading_dates or [])
                            if value.startswith(month_prefix)
                            and segment_start <= value <= segment_end
                        }
                    expected_dates -= (suspended_dates_by_symbol or {}).get(ts_code, set())

                    if not expected_dates:
                        if current.month == 12:
                            current = current.replace(year=current.year + 1, month=1)
                        else:
                            current = current.replace(month=current.month + 1)
                        continue

                    p = _partition_path(root, freq, ts_code, current.year, current.month)
                    if not p.exists():
                        all_complete = False
                        break
                    try:
                        existing_df = pl.read_parquet(p)
                        segment_frame = existing_df.filter(
                                (pl.col("trade_date") >= segment_start)
                                & (pl.col("trade_date") <= segment_end)
                        )
                        actual_dates = set(
                            segment_frame["trade_date"].unique().to_list()
                        )
                        if not expected_dates.issubset(actual_dates):
                            all_complete = False
                            break
                        expected_bar_count = _EXPECTED_BARS_PER_DAY.get(freq)
                        if expected_bar_count is not None:
                            counts = {
                                str(row["trade_date"]): int(row["bar_count"])
                                for row in (
                                    segment_frame.group_by("trade_date")
                                    .agg(pl.len().alias("bar_count"))
                                    .iter_rows(named=True)
                                )
                            }
                            if any(
                                counts.get(value, 0) < expected_bar_count
                                for value in expected_dates
                            ):
                                all_complete = False
                                break
                    except Exception:
                        all_complete = False
                        break
                    if current.month == 12:
                        current = current.replace(year=current.year + 1, month=1)
                    else:
                        current = current.replace(month=current.month + 1)
                if all_complete:
                    symbol_skipped += 1
                    continue

            try:
                data = _fetch_symbol_segment(
                    pro, ts_code, freq, seg_start, seg_end, request_interval
                )
                if data is not None and data.height > 0:
                    symbol_rows += data.height
                    # Group by year-month and write partitions
                    for (year, month), group in data.group_by(
                        [
                            pl.col("trade_date").str.slice(0, 4).cast(pl.Int32).alias("year"),
                            pl.col("trade_date").str.slice(4, 2).cast(pl.Int32).alias("month"),
                        ]
                    ):
                        target = _partition_path(root, freq, ts_code, year, month)
                        merged = _merge_partition(target, group)
                        _atomic_parquet(merged, target)
                        symbol_written += 1
                time.sleep(request_interval)
            except Exception as error:
                symbol_failures.append(
                    {
                        "ts_code": ts_code,
                        "segment": f"{seg_start}/{seg_end}",
                        "error": _redact_error(error)[:300],
                    }
                )
        return symbol_rows, symbol_written, symbol_skipped, symbol_failures

    if max_workers == 1:
        pro = build_tushare_client(env_file)
        symbol_results = [
            (ts_code, sync_symbol(ts_code, pro)) for ts_code in ts_codes
        ]
    else:
        from concurrent.futures import ThreadPoolExecutor

        def sync_with_own_client(
            ts_code: str,
        ) -> tuple[int, int, int, list[dict[str, str]]]:
            return sync_symbol(ts_code, build_tushare_client(env_file))

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            results = executor.map(sync_with_own_client, ts_codes)
            symbol_results = list(zip(ts_codes, results, strict=True))

    for ts_code, (
        symbol_rows,
        symbol_written,
        symbol_skipped,
        symbol_failures,
    ) in symbol_results:
        if symbol_rows > 0:
            synced_symbols += 1
            total_rows += symbol_rows
        written += symbol_written
        skipped += symbol_skipped
        report.failures.extend(symbol_failures)
        if symbol_failures:
            _write_sync_error(
                root,
                ts_code,
                symbol_failures[-1]["error"],
            )
        print(f"  {ts_code}: {symbol_rows} rows")

    report.completed_at = datetime.now(UTC).isoformat()
    report.symbols_synced = synced_symbols
    report.total_rows = total_rows
    report.written_partitions = written
    report.skipped_partitions = skipped
    report.passed = len(report.failures) == 0 and (
        synced_symbols > 0 or skipped > 0
    )

    # Write meta
    meta = root / "meta"
    meta.mkdir(parents=True, exist_ok=True)
    (meta / "last-sync.json").write_text(
        json.dumps(asdict(report), ensure_ascii=False, indent=2) + "\n", "utf-8"
    )
    if not report.failures:
        error_file = meta / "last-sync-error.json"
        if error_file.exists():
            error_file.unlink()

    return report


def _write_sync_error(root: Path, ts_code: str, error: str) -> None:
    meta = root / "meta"
    meta.mkdir(parents=True, exist_ok=True)
    (meta / "last-sync-error.json").write_text(
        json.dumps(
            {
                "status": "failed",
                "ts_code": ts_code,
                "error": _redact_error(error)[:500],
                "recorded_at": datetime.now(UTC).isoformat(),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        "utf-8",
    )


# ---------------------------------------------------------------------------
# Coverage report
# ---------------------------------------------------------------------------


def compute_coverage(
    root: Path | str,
    start_date: str,
    end_date: str,
    freq: str = "5min",
    trading_dates: list[str] | None = None,
    daily_volume_map: dict[str, set[str]] | None = None,
    suspended_map: dict[str, set[str]] | None = None,
    expected_symbols: list[str] | None = None,
) -> dict[str, Any]:
    """Compute and persist the same fail-closed report used by the CLI gate."""
    from .minute_quality import run_minute_quality

    root = Path(root)
    report = run_minute_quality(
        root,
        start_date,
        end_date,
        freq=freq,
        trading_dates=trading_dates,
        daily_volume_map=daily_volume_map,
        suspended_map=suspended_map,
        expected_symbols=expected_symbols,
    )
    result = report.to_dict()
    meta = root / "meta"
    meta.mkdir(parents=True, exist_ok=True)
    (meta / "coverage.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n",
        "utf-8",
    )
    return result


def load_trading_dates(
    data_root: Path | str,
    start_date: str,
    end_date: str,
) -> list[str] | None:
    """Load authoritative open days; never infer them from existing partitions."""
    target = Path(data_root) / "reference" / "trade_cal.parquet"
    if not target.exists() or target.stat().st_size == 0:
        return None
    frame = pl.read_parquet(target)
    if "cal_date" not in frame.columns:
        return None
    if "is_open" in frame.columns:
        frame = frame.filter(pl.col("is_open").cast(pl.Int64) == 1)
    start = start_date.replace("-", "")
    end = end_date.replace("-", "")
    values = (
        frame.with_columns(pl.col("cal_date").cast(pl.String))
        .filter((pl.col("cal_date") >= start) & (pl.col("cal_date") <= end))
        ["cal_date"]
        .unique()
        .sort()
        .to_list()
    )
    return values or None


def load_daily_volume_map(
    data_root: Path | str,
    start_date: str,
    end_date: str,
) -> dict[str, set[str]]:
    """Return dates with positive daily volume using the Tushare `vol` field."""
    root = Path(data_root) / "raw" / "daily"
    start = start_date.replace("-", "")
    end = end_date.replace("-", "")
    result: dict[str, set[str]] = {}
    if not root.exists():
        return result
    for partition in sorted(root.glob("trade_date=*/part.parquet")):
        trade_date = partition.parent.name.removeprefix("trade_date=")
        if not start <= trade_date <= end:
            continue
        frame = pl.read_parquet(partition)
        volume_column = "vol" if "vol" in frame.columns else "volume"
        if volume_column not in frame.columns or "ts_code" not in frame.columns:
            continue
        for ts_code in (
            frame.filter(pl.col(volume_column).cast(pl.Float64, strict=False) > 0)
            ["ts_code"]
            .cast(pl.String)
            .to_list()
        ):
            result.setdefault(ts_code, set()).add(trade_date)
    return result


def load_suspended_map(
    data_root: Path | str,
    start_date: str,
    end_date: str,
) -> dict[str, set[str]]:
    """Load point-in-time suspension dates from the dedicated endpoint."""
    root = Path(data_root) / "raw" / "suspend_d"
    start = start_date.replace("-", "")
    end = end_date.replace("-", "")
    result: dict[str, set[str]] = {}
    if not root.exists():
        return result
    for partition in sorted(root.glob("trade_date=*/part.parquet")):
        trade_date = partition.parent.name.removeprefix("trade_date=")
        if not start <= trade_date <= end:
            continue
        frame = pl.read_parquet(partition)
        if "ts_code" not in frame.columns:
            continue
        for ts_code in frame["ts_code"].cast(pl.String).to_list():
            result.setdefault(ts_code, set()).add(trade_date)
    return result


def _valid_5min_times() -> list[str]:
    """Generate valid 5-min bar close timestamps for A-share sessions."""
    times: list[str] = []
    # AM: 09:35, 09:40, ..., 11:30
    h, m = 9, 35
    while (h, m) <= (11, 30):
        times.append(f"{h:02d}:{m:02d}:00")
        m += 5
        if m >= 60:
            h += 1
            m -= 60
    # PM: 13:05, 13:10, ..., 15:00
    h, m = 13, 5
    while (h, m) <= (15, 0):
        times.append(f"{h:02d}:{m:02d}:00")
        m += 5
        if m >= 60:
            h += 1
            m -= 60
    return times


def load_minute_bars(
    root: Path | str,
    ts_code: str,
    start_date: str,
    end_date: str,
    freq: str = "5min",
) -> pl.DataFrame:
    """Load minute bars for a symbol and date range from the warehouse.

    Args:
        root: Minute data root.
        ts_code: e.g. '000001.SZ'.
        start_date: YYYYMMDD start.
        end_date: YYYYMMDD end.
        freq: Bar frequency.

    Returns:
        DataFrame sorted by trade_time ascending.
    """
    root = Path(root)
    freq_root = root / "raw" / f"freq={freq}" / f"ts_code={ts_code}"
    if not freq_root.exists():
        return pl.DataFrame()

    frames: list[pl.DataFrame] = []
    for parquet_file in sorted(freq_root.rglob("part.parquet")):
        if parquet_file.stat().st_size > 0:
            df = pl.read_parquet(parquet_file)
            # Filter by date range
            df = df.filter(
                (pl.col("trade_date") >= start_date) & (pl.col("trade_date") <= end_date)
            )
            if df.height > 0:
                frames.append(df)

    if not frames:
        return pl.DataFrame()

    result = pl.concat(frames, how="diagonal_relaxed")
    result = result.unique(subset=["ts_code", "trade_time", "freq"], keep="last")
    return result.sort("trade_time")
