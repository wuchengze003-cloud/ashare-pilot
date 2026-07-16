from pathlib import Path

import polars as pl
import pytest

from ashare_research.data_sync import (
    assert_production_dataset,
    inspect_dataset_coverage,
    write_dataset_coverage,
)

REQUIRED = ("daily", "adj_factor", "daily_basic", "moneyflow", "stk_limit")


def write_partition(root: Path, endpoint: str, trade_date: str) -> None:
    target = root / "raw" / endpoint / f"trade_date={trade_date}" / "part.parquet"
    target.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({"trade_date": [trade_date]}).write_parquet(target)


def write_references(root: Path) -> None:
    reference = root / "reference"
    reference.mkdir(parents=True)
    for name in ("stock_basic", "namechange"):
        pl.DataFrame({"value": [name]}).write_parquet(reference / f"{name}.parquet")


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
