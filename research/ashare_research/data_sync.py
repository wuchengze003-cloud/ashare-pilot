"""Tushare to partitioned Parquet ingestion for point-in-time research."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl
import tushare as ts
from dotenv import load_dotenv
from tushare.pro import client as ts_client

DAILY_ENDPOINTS = (
    ("daily", True),
    ("adj_factor", True),
    ("daily_basic", True),
    ("moneyflow", True),
    ("stk_limit", True),
    ("limit_list_d", False),
    ("suspend_d", True),
)
CSI800_COMPONENTS = {
    "000300.SH": 300,
    "000905.SH": 500,
}
MAX_INDEX_SNAPSHOT_AGE_DAYS = 45


@dataclass(frozen=True)
class SyncFailure:
    endpoint: str
    trade_date: str
    error: str


@dataclass(frozen=True)
class SyncReport:
    started_at: str
    completed_at: str
    start_date: str
    end_date: str
    trading_days: int
    written_partitions: int
    skipped_partitions: int
    failures: tuple[SyncFailure, ...]
    dataset_coverage_passed: bool

    @property
    def data_quality_passed(self) -> bool:
        required = {name for name, is_required in DAILY_ENDPOINTS if is_required}
        return self.dataset_coverage_passed and not any(
            failure.endpoint in required for failure in self.failures
        )


@dataclass(frozen=True)
class DatasetCoverage:
    source: str
    generated_at: str
    passed: bool
    start_date: str | None
    end_date: str | None
    trading_days: int
    common_required_days: int
    endpoint_days: dict[str, int]
    missing_required_days: dict[str, tuple[str, ...]]
    reference_tables: dict[str, bool]
    failures: tuple[str, ...]


def _partition_dates(root: Path, endpoint: str) -> set[str]:
    endpoint_root = root / "raw" / endpoint
    if not endpoint_root.exists():
        return set()
    result: set[str] = set()
    for file in endpoint_root.glob("trade_date=*/part.parquet"):
        value = file.parent.name.removeprefix("trade_date=")
        if len(value) == 8 and value.isdigit() and file.stat().st_size > 0:
            result.add(value)
    return result


def inspect_dataset_coverage(root: Path | str) -> DatasetCoverage:
    root = Path(root)
    required = [name for name, is_required in DAILY_ENDPOINTS if is_required]
    endpoint_dates = {name: _partition_dates(root, name) for name, _ in DAILY_ENDPOINTS}
    daily_dates = endpoint_dates["daily"]
    missing = {
        name: tuple(sorted(daily_dates - endpoint_dates[name]))
        for name in required
        if daily_dates - endpoint_dates[name]
    }
    common = set.intersection(*(endpoint_dates[name] for name in required)) if daily_dates else set()
    references = {
        name: (
            (root / "reference" / f"{name}.parquet").is_file()
            and (root / "reference" / f"{name}.parquet").stat().st_size > 0
        )
        for name in (
            "stock_basic",
            "namechange",
            "trade_cal",
            "sw_industry_membership",
            "csi800_index_weight",
            "csi800_membership",
        )
    }
    failures: list[str] = []
    if not daily_dates:
        failures.append("daily partitions are missing")
    for name in required:
        if not endpoint_dates[name]:
            failures.append(f"required endpoint has no partitions: {name}")
    for name, dates in missing.items():
        failures.append(f"{name} missing {len(dates)} daily partitions")
    for name, present in references.items():
        if not present:
            failures.append(f"reference table is missing: {name}")
    csi800_intervals = root / "reference" / "csi800.txt"
    if not csi800_intervals.is_file() or csi800_intervals.stat().st_size == 0:
        failures.append("reference table is missing: csi800.txt")
    trade_cal_path = root / "reference" / "trade_cal.parquet"
    if trade_cal_path.is_file():
        try:
            trade_cal = pl.read_parquet(trade_cal_path)
            if "cal_date" not in trade_cal.columns:
                failures.append("trade calendar is missing cal_date")
            else:
                if "is_open" in trade_cal.columns:
                    trade_cal = trade_cal.filter(
                        pl.col("is_open").cast(pl.Int64, strict=False) == 1
                    )
                calendar_dates = set(
                    trade_cal["cal_date"].cast(pl.String).to_list()
                )
                invalid_calendar_dates = {
                    value
                    for value in calendar_dates
                    if len(value) != 8 or not value.isdigit()
                }
                if invalid_calendar_dates:
                    failures.append(
                        "trade calendar contains invalid cal_date values"
                    )
                missing_calendar_days = sorted(daily_dates - calendar_dates)
                if missing_calendar_days:
                    failures.append(
                        "trade calendar missing "
                        f"{len(missing_calendar_days)} daily trading days"
                    )
                missing_daily_days = tuple(sorted(calendar_dates - daily_dates))
                if missing_daily_days:
                    missing["daily"] = missing_daily_days
                    failures.append(
                        "daily missing "
                        f"{len(missing_daily_days)} open-calendar partitions"
                    )
        except Exception as error:
            failures.append(f"trade calendar is unreadable: {error}")
    ordered = sorted(daily_dates)
    return DatasetCoverage(
        source="tushare-pro-point-in-time",
        generated_at=datetime.now(UTC).isoformat(),
        passed=not failures,
        start_date=ordered[0] if ordered else None,
        end_date=ordered[-1] if ordered else None,
        trading_days=len(daily_dates),
        common_required_days=len(common),
        endpoint_days={name: len(values) for name, values in endpoint_dates.items()},
        missing_required_days=missing,
        reference_tables=references,
        failures=tuple(failures),
    )


def write_dataset_coverage(root: Path | str) -> DatasetCoverage:
    root = Path(root)
    coverage = inspect_dataset_coverage(root)
    meta = root / "meta"
    meta.mkdir(parents=True, exist_ok=True)
    (meta / "coverage.json").write_text(
        json.dumps(asdict(coverage), ensure_ascii=False, indent=2) + "\n",
        "utf-8",
    )
    return coverage


def assert_production_dataset(
    root: Path | str,
    panel_start: str,
    panel_end: str,
    minimum_trading_days: int = 1_500,
) -> DatasetCoverage:
    coverage = inspect_dataset_coverage(root)
    failures = list(coverage.failures)
    normalized_start = panel_start.replace("-", "")
    normalized_end = panel_end.replace("-", "")
    if coverage.start_date is None or coverage.start_date > normalized_start:
        failures.append(
            f"dataset starts after panel: coverage={coverage.start_date}, panel={normalized_start}"
        )
    if coverage.end_date is None or coverage.end_date < normalized_end:
        failures.append(
            f"dataset ends before panel: coverage={coverage.end_date}, panel={normalized_end}"
        )
    if coverage.common_required_days < minimum_trading_days:
        failures.append(
            f"only {coverage.common_required_days} complete trading days; "
            f"require {minimum_trading_days}"
        )
    if failures:
        raise ValueError("production dataset admission failed: " + "; ".join(failures))
    return coverage


def _patch_tushare_url(url: str, target: Any) -> None:
    patched = False
    for attr in dir(target):
        if "http_url" not in attr and not attr.endswith("__url"):
            continue
        try:
            setattr(target, attr, url)
            patched = True
        except Exception:
            pass
    if not patched:
        raise RuntimeError("unable to apply TUSHARE_HTTP_URL to installed tushare client")


def build_tushare_client(env_file: Path | None = None):
    if env_file:
        load_dotenv(env_file)
    token = os.environ.get("TUSHARE_TOKEN")
    if not token:
        raise RuntimeError("TUSHARE_TOKEN is required")
    custom_url = os.environ.get("TUSHARE_HTTP_URL")
    if custom_url:
        _patch_tushare_url(custom_url, ts_client.DataApi)
    ts.set_token(token)
    pro = ts.pro_api()
    if custom_url:
        _patch_tushare_url(custom_url, pro)
    return pro


def _atomic_parquet(frame: pl.DataFrame, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=target.name, suffix=".tmp", dir=target.parent)
    os.close(fd)
    try:
        frame.write_parquet(temporary, compression="zstd")
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _atomic_text(value: str, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=target.name,
        suffix=".tmp",
        dir=target.parent,
    )
    os.close(fd)
    try:
        Path(temporary).write_text(value, "utf-8")
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _normalize_frame(frame: pl.DataFrame) -> pl.DataFrame:
    """Normalize dtypes so partitions written at different times stay scan-compatible.

    Tushare/pandas infers dtypes per response payload, so the same column can
    land as Int64 in one partition and Float64 in another (observed: daily
    `vol` on trade_date=20260608). Cast every numeric column to Float64 and
    leave non-numeric columns untouched.
    """
    numeric = (
        pl.Int8, pl.Int16, pl.Int32, pl.Int64,
        pl.UInt8, pl.UInt16, pl.UInt32, pl.UInt64,
        pl.Float32, pl.Float64,
    )
    return frame.with_columns(
        pl.col(name).cast(pl.Float64)
        for name, dtype in zip(frame.columns, frame.dtypes, strict=True)
        if dtype in numeric
    )


def _month_starts(start_date: date, end_date: date) -> tuple[date, ...]:
    if end_date < start_date:
        return ()
    current = start_date.replace(day=1)
    result: list[date] = []
    while current <= end_date:
        result.append(current)
        current = (
            date(current.year + 1, 1, 1)
            if current.month == 12
            else date(current.year, current.month + 1, 1)
        )
    return tuple(result)


def _ts_code_to_instrument(value: str) -> str:
    code, exchange = value.split(".", maxsplit=1)
    if len(code) != 6 or exchange not in {"SH", "SZ", "BJ"}:
        raise ValueError(f"invalid constituent code: {value!r}")
    return f"{exchange}{code}"


def _validated_index_snapshots(frame: pl.DataFrame) -> pl.DataFrame:
    required = {"index_code", "trade_date", "con_code", "weight"}
    missing = required - set(frame.columns)
    if missing:
        raise RuntimeError(
            f"index_weight missing required columns: {sorted(missing)}"
        )
    normalized = (
        frame.select(sorted(required))
        .with_columns(
            pl.col("index_code").cast(pl.String),
            pl.col("trade_date").cast(pl.String),
            pl.col("con_code").cast(pl.String),
            pl.col("weight").cast(pl.Float64, strict=False),
        )
        .drop_nulls(["index_code", "trade_date", "con_code", "weight"])
        .unique(
            subset=["index_code", "trade_date", "con_code"],
            keep="last",
        )
        .sort("index_code", "trade_date", "con_code")
    )
    if normalized.is_empty():
        raise RuntimeError("index_weight history is empty")
    unknown = set(normalized["index_code"].unique()) - set(CSI800_COMPONENTS)
    if unknown:
        raise RuntimeError(f"unexpected index_weight sources: {sorted(unknown)}")
    invalid_dates = normalized.filter(
        ~pl.col("trade_date").str.contains(r"^\d{8}$")
    )
    if not invalid_dates.is_empty():
        raise RuntimeError("index_weight contains invalid trade_date")
    grouped = normalized.group_by("index_code", "trade_date").agg(
        pl.col("con_code").n_unique().alias("constituents"),
        pl.col("weight").sum().alias("weight_sum"),
    )
    for row in grouped.iter_rows(named=True):
        expected = CSI800_COMPONENTS[row["index_code"]]
        if row["constituents"] != expected:
            raise RuntimeError(
                "index_weight snapshot has wrong constituent count: "
                f"{row['index_code']} {row['trade_date']} "
                f"{row['constituents']} != {expected}"
            )
        if not 99.0 <= row["weight_sum"] <= 101.0:
            raise RuntimeError(
                "index_weight snapshot has invalid total weight: "
                f"{row['index_code']} {row['trade_date']} "
                f"{row['weight_sum']:.4f}"
            )
    return normalized


def _build_csi800_membership(
    snapshots: pl.DataFrame,
    start_date: date,
    end_date: date,
) -> pl.DataFrame:
    if end_date < start_date:
        raise ValueError("membership end date precedes start date")
    source_snapshots: dict[str, dict[date, set[str]]] = {}
    for index_code, expected in CSI800_COMPONENTS.items():
        source = snapshots.filter(pl.col("index_code") == index_code)
        by_date: dict[date, set[str]] = {}
        for trade_date, group in source.partition_by(
            "trade_date",
            as_dict=True,
        ).items():
            raw_date = trade_date[0] if isinstance(trade_date, tuple) else trade_date
            snapshot_date = date.fromisoformat(
                f"{str(raw_date)[:4]}-{str(raw_date)[4:6]}-{str(raw_date)[6:]}"
            )
            members = {
                _ts_code_to_instrument(value)
                for value in group["con_code"].to_list()
            }
            if len(members) != expected:
                raise RuntimeError(
                    f"{index_code} {snapshot_date} membership is incomplete"
                )
            by_date[snapshot_date] = members
        eligible_starts = [value for value in by_date if value <= start_date]
        if not eligible_starts:
            raise RuntimeError(
                f"{index_code} has no snapshot at or before {start_date}"
            )
        ordered = sorted(value for value in by_date if value <= end_date)
        if (start_date - max(eligible_starts)).days > MAX_INDEX_SNAPSHOT_AGE_DAYS:
            raise RuntimeError(
                f"{index_code} membership is stale at start date {start_date}"
            )
        if not ordered or (end_date - ordered[-1]).days > MAX_INDEX_SNAPSHOT_AGE_DAYS:
            raise RuntimeError(
                f"{index_code} membership is stale at end date {end_date}"
            )
        for left, right in zip(ordered, ordered[1:], strict=False):
            if (right - left).days > MAX_INDEX_SNAPSHOT_AGE_DAYS:
                raise RuntimeError(
                    f"{index_code} membership has a snapshot gap: {left} -> {right}"
                )
        source_snapshots[index_code] = by_date

    event_dates = {
        start_date,
        *(
            value
            for by_date in source_snapshots.values()
            for value in by_date
            if start_date < value <= end_date
        ),
    }
    timeline: list[tuple[date, set[str]]] = []
    for event_date in sorted(event_dates):
        component_sets: list[set[str]] = []
        for index_code in CSI800_COMPONENTS:
            by_date = source_snapshots[index_code]
            snapshot_date = max(value for value in by_date if value <= event_date)
            component_sets.append(by_date[snapshot_date])
        overlap = component_sets[0] & component_sets[1]
        members = component_sets[0] | component_sets[1]
        if overlap or len(members) != 800:
            raise RuntimeError(
                "CSI800 union is invalid at "
                f"{event_date}: overlap={len(overlap)} union={len(members)}"
            )
        if not timeline or timeline[-1][1] != members:
            timeline.append((event_date, members))

    active_starts: dict[str, date] = {}
    intervals: list[dict[str, Any]] = []
    for event_date, members in timeline:
        previous = set(active_starts)
        for instrument in sorted(previous - members):
            intervals.append(
                {
                    "instrument": instrument,
                    "member_start": active_starts.pop(instrument),
                    "member_end": event_date - timedelta(days=1),
                }
            )
        for instrument in sorted(members - previous):
            active_starts[instrument] = event_date
    intervals.extend(
        {
            "instrument": instrument,
            "member_start": member_start,
            "member_end": end_date,
        }
        for instrument, member_start in sorted(active_starts.items())
    )
    result = pl.DataFrame(
        intervals,
        schema={
            "instrument": pl.String,
            "member_start": pl.Date,
            "member_end": pl.Date,
        },
    ).sort("instrument", "member_start")
    if result.is_empty():
        raise RuntimeError("CSI800 membership intervals are empty")
    return result


def _write_csi800_membership(
    pro,
    reference: Path,
    start_date: date,
    end_date: date,
    refresh: bool,
    rate_limiter: _RequestRateLimiter,
) -> None:
    snapshots_target = reference / "csi800_index_weight.parquet"
    existing = (
        pl.read_parquet(snapshots_target)
        if snapshots_target.exists() and snapshots_target.stat().st_size > 0
        else None
    )
    query_start = start_date - timedelta(days=45)
    if existing is not None and not refresh:
        latest = max(
            date.fromisoformat(
                f"{value[:4]}-{value[4:6]}-{value[6:]}"
            )
            for value in existing["trade_date"].cast(pl.String).to_list()
        )
        query_start = max(query_start, latest - timedelta(days=45))

    fetched: list[pl.DataFrame] = []
    for month_start in _month_starts(query_start, end_date):
        next_month = (
            date(month_start.year + 1, 1, 1)
            if month_start.month == 12
            else date(month_start.year, month_start.month + 1, 1)
        )
        month_end = min(end_date, next_month - timedelta(days=1))
        for index_code in CSI800_COMPONENTS:
            frame = _query_with_retry(
                pro,
                "index_weight",
                index_code=index_code,
                start_date=month_start.strftime("%Y%m%d"),
                end_date=month_end.strftime("%Y%m%d"),
                rate_limiter=rate_limiter,
            )
            if frame is None or frame.empty:
                continue
            fetched.append(
                pl.from_pandas(frame).with_columns(
                    pl.lit(index_code).alias("index_code")
                )
            )
    if not fetched and existing is None:
        raise RuntimeError("index_weight returned no CSI300 or CSI500 snapshots")
    combined = pl.concat(
        ([existing] if existing is not None else []) + fetched,
        how="diagonal_relaxed",
    )
    snapshots = _validated_index_snapshots(combined)
    membership = _build_csi800_membership(
        snapshots,
        start_date,
        end_date,
    )
    _atomic_parquet(snapshots, snapshots_target)
    _atomic_parquet(membership, reference / "csi800_membership.parquet")
    interval_lines = "\n".join(
        f"{row['instrument']}\t{row['member_start']}\t{row['member_end']}"
        for row in membership.iter_rows(named=True)
    )
    _atomic_text(interval_lines + "\n", reference / "csi800.txt")
    manifest = {
        "schema_version": 1,
        "source": "tushare-index-weight-csi300-plus-csi500",
        "source_indices": CSI800_COMPONENTS,
        "generated_at": datetime.now(UTC).isoformat(),
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "snapshot_rows": snapshots.height,
        "snapshot_dates": snapshots["trade_date"].n_unique(),
        "membership_intervals": membership.height,
        "active_members_at_end": membership.filter(
            pl.col("member_end") == end_date
        )["instrument"].n_unique(),
    }
    _atomic_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        reference / "csi800.manifest.json",
    )


class _RequestRateLimiter:
    """Thread-safe minimum interval between provider request starts."""

    def __init__(self, interval_seconds: float) -> None:
        if interval_seconds < 0:
            raise ValueError("request interval must be non-negative")
        self._interval = interval_seconds
        self._lock = threading.Lock()
        self._next_request = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            delay = max(0.0, self._next_request - now)
            self._next_request = max(now, self._next_request) + self._interval
        if delay > 0:
            time.sleep(delay)


def _query_with_retry(
    pro,
    endpoint: str,
    attempts: int = 5,
    rate_limiter: _RequestRateLimiter | None = None,
    require_nonempty: bool = False,
    **kwargs,
):
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            if rate_limiter is not None:
                rate_limiter.wait()
            frame = pro.query(endpoint, **kwargs)
            if require_nonempty and (
                frame is None or getattr(frame, "empty", True)
            ):
                raise RuntimeError("provider returned an empty response")
            return frame
        except Exception as error:
            last_error = error
            if attempt + 1 < attempts:
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"{endpoint} failed after {attempts} attempts: {last_error}")


def _write_sw_industry_membership(
    pro,
    reference: Path,
    refresh: bool,
    rate_limiter: _RequestRateLimiter,
) -> None:
    target = reference / "sw_industry_membership.parquet"
    if target.exists() and not refresh:
        return
    classification_frames = []
    for source in ("SW2014", "SW2021"):
        for level in ("L1", "L2", "L3"):
            frame = _query_with_retry(
                pro,
                "index_classify",
                level=level,
                src=source,
                rate_limiter=rate_limiter,
                require_nonempty=True,
            )
            if frame is not None and not frame.empty:
                classification_frames.append(frame)
    if not classification_frames:
        raise RuntimeError("index_classify returned no SW classifications")

    frames = []
    for status in ("Y", "N"):
        frame = _query_with_retry(
            pro,
            "index_member_all",
            is_new=status,
            rate_limiter=rate_limiter,
        )
        if frame is not None and not frame.empty:
            frames.append(frame)
    if not frames:
        raise RuntimeError("index_member_all returned no historical memberships")

    import pandas as pd

    def clean_cell(value: Any) -> str:
        if value is None or pd.isna(value):
            return ""
        return str(value).strip()

    classifications = pd.concat(classification_frames, ignore_index=True)
    required_classifications = {
        "index_code",
        "industry_name",
        "level",
        "industry_code",
        "parent_code",
    }
    missing_classifications = required_classifications - set(
        classifications.columns
    )
    if missing_classifications:
        raise RuntimeError(
            "index_classify missing required columns: "
            f"{sorted(missing_classifications)}"
        )
    classifications = classifications.dropna(subset=["industry_code"])
    index_to_industry = {
        clean_cell(row.index_code): clean_cell(row.industry_code)
        for row in classifications.itertuples()
        if clean_cell(row.index_code) and clean_cell(row.industry_code)
    }
    parent_by_industry = {
        clean_cell(row.industry_code): clean_cell(row.parent_code)
        for row in classifications.itertuples()
        if clean_cell(row.industry_code) and clean_cell(row.parent_code)
    }
    l1_by_industry = {
        clean_cell(row.industry_code): (
            clean_cell(row.index_code),
            clean_cell(row.industry_name),
        )
        for row in classifications[classifications["level"] == "L1"].itertuples()
        if clean_cell(row.industry_code)
        and clean_cell(row.index_code)
        and clean_cell(row.industry_name)
    }

    raw_membership = pd.concat(frames, ignore_index=True)
    if len(raw_membership) == 2_000:
        raise RuntimeError(
            "index_member_all may be truncated at the documented row limit"
    )
    mapped_rows = []
    for row in raw_membership.to_dict("records"):
        l1_code = clean_cell(row.get("l1_code"))
        l1_name = clean_cell(row.get("l1_name"))
        if not l1_code or not l1_name:
            l2_value = clean_cell(row.get("l2_code"))
            l2_industry = index_to_industry.get(l2_value, l2_value)
            l1_industry = parent_by_industry.get(l2_industry)
            if l1_industry in l1_by_industry:
                l1_code, l1_name = l1_by_industry[l1_industry]
        if not l1_code or not l1_name:
            l3_value = clean_cell(row.get("l3_code"))
            l3_industry = index_to_industry.get(l3_value, l3_value)
            l2_industry = parent_by_industry.get(l3_industry)
            l1_industry = parent_by_industry.get(
                clean_cell(l2_industry)
            )
            if l1_industry in l1_by_industry:
                l1_code, l1_name = l1_by_industry[l1_industry]
        mapped_rows.append(
            {
                "ts_code": clean_cell(row.get("ts_code")) or None,
                "l1_code": l1_code or None,
                "l1_name": l1_name or None,
                "in_date": clean_cell(row.get("in_date")) or None,
                "out_date": clean_cell(row.get("out_date")) or None,
                "is_new": clean_cell(row.get("is_new")) or None,
            }
        )
    membership = pl.from_dicts(mapped_rows)
    required = {"ts_code", "l1_code", "l1_name", "in_date", "out_date"}
    missing = required - set(membership.columns)
    if missing:
        raise RuntimeError(
            f"index_member_all missing required columns: {sorted(missing)}"
        )
    membership = (
        membership.with_columns(
            pl.col("ts_code").cast(pl.String),
            pl.col("l1_code").cast(pl.String),
            pl.col("l1_name").cast(pl.String),
            pl.col("in_date").cast(pl.String),
            pl.col("out_date").cast(pl.String),
        )
        .drop_nulls(["ts_code", "l1_code", "in_date"])
        .filter(
            (pl.col("l1_code").str.len_chars() > 0)
            & (pl.col("l1_name").str.len_chars() > 0)
        )
        .unique(
            subset=["ts_code", "l1_code", "in_date", "out_date"],
            keep="last",
        )
        .sort("ts_code", "in_date")
    )
    if membership.is_empty():
        raise RuntimeError("SW membership is empty after validation")
    _atomic_parquet(membership, target)
    _atomic_text(
        json.dumps(
            {
                "schema_version": 1,
                "source": "tushare-index-member-all",
                "generated_at": datetime.now(UTC).isoformat(),
                "rows": membership.height,
                "symbols": membership["ts_code"].n_unique(),
                "historical_rows": membership.filter(
                    pl.col("is_new") == "N"
                ).height
                if "is_new" in membership.columns
                else 0,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        reference / "sw_industry_membership.manifest.json",
    )


def _write_reference_tables(
    pro,
    root: Path,
    start_date: date,
    end_date: date,
    refresh: bool,
    rate_limiter: _RequestRateLimiter,
) -> None:
    reference = root / "reference"
    endpoints = ("stock_basic", "namechange")
    for endpoint in endpoints:
        target = reference / f"{endpoint}.parquet"
        if target.exists() and not refresh:
            continue
        if endpoint == "stock_basic":
            frames = [
                _query_with_retry(
                    pro,
                    "stock_basic",
                    exchange="",
                    list_status=status,
                    fields="ts_code,symbol,name,area,industry,market,list_date,delist_date,list_status",
                    rate_limiter=rate_limiter,
                )
                for status in ("L", "D", "P")
            ]
        else:
            frames = [
                _query_with_retry(
                    pro,
                    "namechange",
                    ts_code="",
                    rate_limiter=rate_limiter,
                )
            ]
        nonempty = [frame for frame in frames if frame is not None and not frame.empty]
        if nonempty:
            import pandas as pd

            _atomic_parquet(pl.from_pandas(pd.concat(nonempty, ignore_index=True)), target)
    _write_sw_industry_membership(
        pro,
        reference,
        refresh,
        rate_limiter,
    )
    _write_csi800_membership(
        pro,
        reference,
        start_date,
        end_date,
        refresh,
        rate_limiter,
    )


def _write_trade_calendar(calendar: Any, root: Path) -> None:
    """Persist the authoritative open-day calendar used by minute gates."""
    if calendar is None or calendar.empty:
        raise RuntimeError("trade_cal returned no open trading dates")
    required_columns = {"cal_date", "is_open"}
    missing_columns = required_columns - set(calendar.columns)
    if missing_columns:
        raise RuntimeError(
            f"trade_cal missing required columns: {sorted(missing_columns)}"
        )

    incoming = pl.from_pandas(calendar).with_columns(
        pl.col("cal_date").cast(pl.String),
        pl.col("is_open").cast(pl.Int8),
    )
    target = root / "reference" / "trade_cal.parquet"
    if target.exists() and target.stat().st_size > 0:
        existing = pl.read_parquet(target).with_columns(
            pl.col("cal_date").cast(pl.String),
            pl.col("is_open").cast(pl.Int8),
        )
        incoming = pl.concat([existing, incoming], how="diagonal_relaxed")
    calendar_frame = (
        incoming.unique(subset=["cal_date"], keep="last")
        .sort("cal_date")
        .select("cal_date", "is_open")
    )
    _atomic_parquet(calendar_frame, target)


def sync_csi800_membership(
    root: Path | str,
    start_date: date,
    end_date: date,
    env_file: Path | None = None,
    refresh: bool = False,
    request_interval_seconds: float = 0.12,
) -> dict[str, Any]:
    root = Path(root)
    pro = build_tushare_client(env_file)
    limiter = _RequestRateLimiter(request_interval_seconds)
    _write_csi800_membership(
        pro,
        root / "reference",
        start_date,
        end_date,
        refresh,
        limiter,
    )
    manifest_path = root / "reference" / "csi800.manifest.json"
    return json.loads(manifest_path.read_text("utf-8"))


def sync_sw_industry_membership(
    root: Path | str,
    env_file: Path | None = None,
    request_interval_seconds: float = 0.12,
) -> dict[str, Any]:
    root = Path(root)
    pro = build_tushare_client(env_file)
    limiter = _RequestRateLimiter(request_interval_seconds)
    _write_sw_industry_membership(
        pro,
        root / "reference",
        True,
        limiter,
    )
    manifest_path = (
        root / "reference" / "sw_industry_membership.manifest.json"
    )
    return json.loads(manifest_path.read_text("utf-8"))


def sync_tushare(
    root: Path | str,
    start_date: date,
    end_date: date,
    env_file: Path | None = None,
    refresh: bool = False,
    request_interval_seconds: float = 0.12,
    max_workers: int = 8,
) -> SyncReport:
    if max_workers <= 0:
        raise ValueError("max_workers must be positive")
    root = Path(root)
    pro = build_tushare_client(env_file)
    started = datetime.now(UTC)
    limiter = _RequestRateLimiter(request_interval_seconds)
    _write_reference_tables(
        pro,
        root,
        start_date,
        end_date,
        refresh,
        limiter,
    )
    calendar = _query_with_retry(
        pro,
        "trade_cal",
        exchange="SSE",
        start_date=start_date.strftime("%Y%m%d"),
        end_date=end_date.strftime("%Y%m%d"),
        is_open="1",
        fields="cal_date,is_open",
        rate_limiter=limiter,
        require_nonempty=True,
    )
    _write_trade_calendar(calendar, root)
    trade_dates = sorted(str(value) for value in calendar["cal_date"].tolist())
    failures: list[SyncFailure] = []
    written = 0
    skipped = 0

    tasks: list[tuple[str, str, bool, Path]] = []
    for trade_date in trade_dates:
        for endpoint, required in DAILY_ENDPOINTS:
            target = (
                root
                / "raw"
                / endpoint
                / f"trade_date={trade_date}"
                / "part.parquet"
            )
            if target.exists() and not refresh:
                skipped += 1
            else:
                tasks.append((trade_date, endpoint, required, target))

    worker_state = threading.local()

    def worker_client():
        client = getattr(worker_state, "client", None)
        if client is None:
            client = build_tushare_client(env_file)
            worker_state.client = client
        return client

    def fetch_partition(
        task: tuple[str, str, bool, Path],
    ) -> tuple[str, str, bool]:
        trade_date, endpoint, required, target = task
        frame = _query_with_retry(
            worker_client(),
            endpoint,
            trade_date=trade_date,
            rate_limiter=limiter,
            require_nonempty=required and endpoint != "suspend_d",
        )
        if frame is None:
            if required:
                raise RuntimeError("required endpoint returned no rows")
            return endpoint, trade_date, False
        if frame.empty:
            if endpoint == "suspend_d":
                _atomic_parquet(
                    pl.DataFrame(
                        schema={
                            "ts_code": pl.String,
                            "trade_date": pl.String,
                        }
                    ),
                    target,
                )
                return endpoint, trade_date, True
            if required:
                raise RuntimeError("required endpoint returned no rows")
            return endpoint, trade_date, False
        _atomic_parquet(_normalize_frame(pl.from_pandas(frame)), target)
        return endpoint, trade_date, True

    with ThreadPoolExecutor(
        max_workers=min(max_workers, max(1, len(tasks))),
        thread_name_prefix="tushare-sync",
    ) as executor:
        future_tasks = {
            executor.submit(fetch_partition, task): task for task in tasks
        }
        for future in as_completed(future_tasks):
            trade_date, endpoint, _, _ = future_tasks[future]
            try:
                _, _, was_written = future.result()
                written += int(was_written)
            except Exception as error:
                failures.append(SyncFailure(endpoint, trade_date, str(error)))

    coverage = write_dataset_coverage(root)
    report = SyncReport(
        started_at=started.isoformat(),
        completed_at=datetime.now(UTC).isoformat(),
        start_date=start_date.isoformat(),
        end_date=end_date.isoformat(),
        trading_days=len(trade_dates),
        written_partitions=written,
        skipped_partitions=skipped,
        failures=tuple(failures),
        dataset_coverage_passed=coverage.passed,
    )
    meta = root / "meta"
    meta.mkdir(parents=True, exist_ok=True)
    (meta / "last-sync.json").write_text(
        json.dumps(
            {**asdict(report), "data_quality_passed": report.data_quality_passed},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        "utf-8",
    )
    return report
