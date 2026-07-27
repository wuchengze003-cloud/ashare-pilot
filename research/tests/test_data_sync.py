from pathlib import Path

import polars as pl
import pytest

from ashare_research.data_sync import (
    _normalize_frame,
    assert_production_dataset,
    inspect_dataset_coverage,
    write_dataset_coverage,
)

REQUIRED = (
    "daily",
    "adj_factor",
    "daily_basic",
    "moneyflow",
    "stk_limit",
    "suspend_d",
)


def write_partition(root: Path, endpoint: str, trade_date: str) -> None:
    target = root / "raw" / endpoint / f"trade_date={trade_date}" / "part.parquet"
    target.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({"trade_date": [trade_date]}).write_parquet(target)


def write_references(root: Path) -> None:
    reference = root / "reference"
    reference.mkdir(parents=True)
    for name in ("stock_basic", "namechange"):
        pl.DataFrame({"value": [name]}).write_parquet(reference / f"{name}.parquet")
    pl.DataFrame(
        {
            "cal_date": ["20260715", "20260716"],
            "is_open": [1, 1],
        }
    ).write_parquet(reference / "trade_cal.parquet")


def test_dataset_coverage_requires_aligned_partitions_and_references(tmp_path):
    for endpoint in REQUIRED:
        write_partition(tmp_path, endpoint, "20260715")
        write_partition(tmp_path, endpoint, "20260716")
    write_references(tmp_path)

    coverage = write_dataset_coverage(tmp_path)

    assert coverage.passed
    assert coverage.start_date == "20260715"
    assert coverage.end_date == "20260716"
    assert coverage.common_required_days == 2
    assert (tmp_path / "meta" / "coverage.json").is_file()


def test_dataset_coverage_fails_when_required_day_is_missing(tmp_path):
    for endpoint in REQUIRED:
        write_partition(tmp_path, endpoint, "20260715")
    write_partition(tmp_path, "daily", "20260716")
    write_references(tmp_path)

    coverage = inspect_dataset_coverage(tmp_path)

    assert not coverage.passed
    assert coverage.missing_required_days["moneyflow"] == ("20260716",)
    with pytest.raises(ValueError, match="production dataset admission failed"):
        assert_production_dataset(
            tmp_path,
            "2026-07-15",
            "2026-07-16",
            minimum_trading_days=2,
        )


def test_dataset_coverage_requires_suspension_evidence_for_every_day(tmp_path):
    for endpoint in REQUIRED:
        write_partition(tmp_path, endpoint, "20260715")
        write_partition(tmp_path, endpoint, "20260716")
    (tmp_path / "raw" / "suspend_d" / "trade_date=20260716" / "part.parquet").unlink()
    write_references(tmp_path)

    coverage = inspect_dataset_coverage(tmp_path)

    assert not coverage.passed
    assert coverage.missing_required_days["suspend_d"] == ("20260716",)


def test_dataset_coverage_fails_when_trade_calendar_misses_daily_day(tmp_path):
    for endpoint in REQUIRED:
        write_partition(tmp_path, endpoint, "20260715")
        write_partition(tmp_path, endpoint, "20260716")
    write_references(tmp_path)
    pl.DataFrame(
        {"cal_date": ["20260715"], "is_open": [1]}
    ).write_parquet(tmp_path / "reference" / "trade_cal.parquet")

    coverage = inspect_dataset_coverage(tmp_path)

    assert not coverage.passed
    assert any("trade calendar missing 1" in failure for failure in coverage.failures)


def test_dataset_coverage_fails_when_open_day_has_no_daily_partition(tmp_path):
    for endpoint in REQUIRED:
        write_partition(tmp_path, endpoint, "20260715")
    write_references(tmp_path)

    coverage = inspect_dataset_coverage(tmp_path)

    assert not coverage.passed
    assert coverage.missing_required_days["daily"] == ("20260716",)
    assert any(
        "daily missing 1 open-calendar partitions" in failure
        for failure in coverage.failures
    )


def test_partition_schema_normalizes_numeric_provider_drift():
    frame = pl.DataFrame(
        {
            "ts_code": ["000001.SZ"],
            "trade_date": ["20260727"],
            "vol": [100],
            "amount": [1234.5],
        }
    )

    normalized = _normalize_frame(frame)

    assert normalized.schema == {
        "ts_code": pl.String,
        "trade_date": pl.String,
        "vol": pl.Float64,
        "amount": pl.Float64,
    }
