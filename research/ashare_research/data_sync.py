"""Tushare to partitioned Parquet ingestion for point-in-time research."""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
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
    ("suspend_d", False),
)


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
        name: (root / "reference" / f"{name}.parquet").is_file()
        for name in ("stock_basic", "namechange")
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


def _query_with_retry(pro, endpoint: str, attempts: int = 3, **kwargs):
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            return pro.query(endpoint, **kwargs)
        except Exception as error:
            last_error = error
            if attempt + 1 < attempts:
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"{endpoint} failed after {attempts} attempts: {last_error}")


def _write_reference_tables(pro, root: Path, refresh: bool) -> None:
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
                )
                for status in ("L", "D", "P")
            ]
        else:
            frames = [_query_with_retry(pro, "namechange", ts_code="")]
        nonempty = [frame for frame in frames if frame is not None and not frame.empty]
        if nonempty:
            import pandas as pd

            _atomic_parquet(pl.from_pandas(pd.concat(nonempty, ignore_index=True)), target)


def sync_tushare(
    root: Path | str,
    start_date: date,
    end_date: date,
    env_file: Path | None = None,
    refresh: bool = False,
    request_interval_seconds: float = 0.12,
) -> SyncReport:
    root = Path(root)
    pro = build_tushare_client(env_file)
    started = datetime.now(UTC)
    _write_reference_tables(pro, root, refresh)
    calendar = _query_with_retry(
        pro,
        "trade_cal",
        exchange="SSE",
        start_date=start_date.strftime("%Y%m%d"),
        end_date=end_date.strftime("%Y%m%d"),
        is_open="1",
        fields="cal_date,is_open",
    )
    trade_dates = sorted(str(value) for value in calendar["cal_date"].tolist())
    failures: list[SyncFailure] = []
    written = 0
    skipped = 0

    for trade_date in trade_dates:
        for endpoint, required in DAILY_ENDPOINTS:
            target = root / "raw" / endpoint / f"trade_date={trade_date}" / "part.parquet"
            if target.exists() and not refresh:
                skipped += 1
                continue
            try:
                frame = _query_with_retry(pro, endpoint, trade_date=trade_date)
                if frame is None or frame.empty:
                    if required:
                        raise RuntimeError("required endpoint returned no rows")
                    continue
                _atomic_parquet(pl.from_pandas(frame), target)
                written += 1
            except Exception as error:
                failures.append(SyncFailure(endpoint, trade_date, str(error)))
            time.sleep(request_interval_seconds)

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
