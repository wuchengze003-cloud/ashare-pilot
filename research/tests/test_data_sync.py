from datetime import date
from pathlib import Path
from threading import Lock

import pandas as pd
import polars as pl
import pytest

from ashare_research.data_sync import (
    _build_csi800_membership,
    _normalize_frame,
    _query_with_retry,
    _RequestRateLimiter,
    _validated_index_snapshots,
    assert_production_dataset,
    inspect_dataset_coverage,
    sync_tushare,
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
            "ts_code": ["000001.SZ"],
            "l1_code": ["801780.SI"],
            "l1_name": ["银行"],
            "in_date": ["19910403"],
            "out_date": [None],
        }
    ).write_parquet(reference / "sw_industry_membership.parquet")
    pl.DataFrame(
        {
            "cal_date": ["20260715", "20260716"],
            "is_open": [1, 1],
        }
    ).write_parquet(reference / "trade_cal.parquet")
    pl.DataFrame(
        {
            "index_code": ["000300.SH", "000905.SH"],
            "trade_date": ["20260715", "20260715"],
            "con_code": ["000001.SZ", "000301.SZ"],
            "weight": [100.0, 100.0],
        }
    ).write_parquet(reference / "csi800_index_weight.parquet")
    pl.DataFrame(
        {
            "instrument": ["SZ000001"],
            "member_start": [date(2026, 7, 15)],
            "member_end": [date(2026, 7, 16)],
        }
    ).write_parquet(reference / "csi800_membership.parquet")
    (reference / "csi800.txt").write_text(
        "SZ000001\t2026-07-15\t2026-07-16\n",
        "utf-8",
    )


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


def test_request_rate_limiter_rejects_negative_interval():
    with pytest.raises(ValueError, match="non-negative"):
        _RequestRateLimiter(-0.1)


def test_required_query_retries_empty_provider_responses(monkeypatch):
    class EmptyThenReady:
        def __init__(self):
            self.calls = 0

        def query(self, _endpoint, **_kwargs):
            self.calls += 1
            if self.calls < 3:
                return pd.DataFrame()
            return pd.DataFrame({"value": [1]})

    provider = EmptyThenReady()
    monkeypatch.setattr("ashare_research.data_sync.time.sleep", lambda _value: None)

    result = _query_with_retry(
        provider,
        "daily",
        attempts=3,
        require_nonempty=True,
    )

    assert provider.calls == 3
    assert len(result) == 1


def _index_snapshot(
    index_code: str,
    trade_date: str,
    start_code: int,
    count: int,
) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "index_code": [index_code] * count,
            "trade_date": [trade_date] * count,
            "con_code": [
                f"{value:06d}.SZ"
                for value in range(start_code, start_code + count)
            ],
            "weight": [100.0 / count] * count,
        }
    )


def test_csi800_membership_is_point_in_time_union_of_csi300_and_csi500():
    changed_csi300 = pl.DataFrame(
        {
            "index_code": ["000300.SH"] * 300,
            "trade_date": ["20260701"] * 300,
            "con_code": [
                *[f"{value:06d}.SZ" for value in range(2, 301)],
                "000801.SZ",
            ],
            "weight": [100.0 / 300] * 300,
        }
    )
    snapshots = _validated_index_snapshots(
        pl.concat(
            [
                _index_snapshot("000300.SH", "20260630", 1, 300),
                _index_snapshot("000905.SH", "20260630", 301, 500),
                changed_csi300,
            ]
        )
    )

    membership = _build_csi800_membership(
        snapshots,
        date(2026, 6, 30),
        date(2026, 7, 27),
    )

    assert (
        membership.filter(pl.col("member_end") == date(2026, 7, 27))[
            "instrument"
        ].n_unique()
        == 800
    )
    removed = membership.filter(pl.col("instrument") == "SZ000001")
    assert removed["member_end"].to_list() == [date(2026, 6, 30)]
    added = membership.filter(pl.col("instrument") == "SZ000801")
    assert added["member_start"].to_list() == [date(2026, 7, 1)]


def test_csi800_membership_rejects_overlapping_component_snapshots():
    snapshots = _validated_index_snapshots(
        pl.concat(
            [
                _index_snapshot("000300.SH", "20260630", 1, 300),
                _index_snapshot("000905.SH", "20260630", 300, 500),
            ]
        )
    )

    with pytest.raises(RuntimeError, match="CSI800 union is invalid"):
        _build_csi800_membership(
            snapshots,
            date(2026, 6, 30),
            date(2026, 7, 27),
        )


def test_sync_is_concurrent_atomic_and_resumable(tmp_path, monkeypatch):
    class FakePro:
        def __init__(self):
            self.lock = Lock()
            self.calls: list[tuple[str, str | None]] = []

        def query(self, endpoint, **kwargs):
            with self.lock:
                self.calls.append((endpoint, kwargs.get("trade_date")))
            if endpoint == "trade_cal":
                return pd.DataFrame(
                    {"cal_date": ["20260715", "20260716"], "is_open": [1, 1]}
                )
            if endpoint == "stock_basic":
                return pd.DataFrame(
                    {
                        "ts_code": ["000001.SZ"],
                        "symbol": ["000001"],
                        "name": ["平安银行"],
                    }
                )
            if endpoint == "namechange":
                return pd.DataFrame(
                    {
                        "ts_code": ["000001.SZ"],
                        "name": ["平安银行"],
                        "start_date": ["19910101"],
                        "end_date": [None],
                    }
                )
            if endpoint == "index_classify":
                level = kwargs["level"]
                values = {
                    "L1": ("801780.SI", "银行", "480000", "0"),
                    "L2": ("801783.SI", "股份制银行Ⅱ", "480300", "480000"),
                    "L3": ("857831.SI", "股份制银行Ⅲ", "480301", "480300"),
                }
                index_code, industry_name, industry_code, parent_code = values[
                    level
                ]
                return pd.DataFrame(
                    {
                        "index_code": [index_code],
                        "industry_name": [industry_name],
                        "level": [level],
                        "industry_code": [industry_code],
                        "parent_code": [parent_code],
                    }
                )
            if endpoint == "index_member_all":
                if kwargs["is_new"] == "N":
                    return pd.DataFrame()
                return pd.DataFrame(
                    {
                        "ts_code": ["000001.SZ", "000002.SZ"],
                        "l1_code": ["801780.SI", float("nan")],
                        "l1_name": ["银行", float("nan")],
                        "l2_code": ["801783.SI", "480300"],
                        "in_date": ["19910403", "19910129"],
                        "out_date": [None, float("nan")],
                        "is_new": ["Y", "Y"],
                    }
                )
            if endpoint == "index_weight":
                count = 300 if kwargs["index_code"] == "000300.SH" else 500
                start_code = 1 if count == 300 else 301
                return pd.DataFrame(
                    {
                        "index_code": [kwargs["index_code"]] * count,
                        "trade_date": [kwargs["end_date"]] * count,
                        "con_code": [
                            f"{value:06d}.SZ"
                            for value in range(start_code, start_code + count)
                        ],
                        "weight": [100.0 / count] * count,
                    }
                )
            if endpoint == "suspend_d":
                return pd.DataFrame()
            return pd.DataFrame(
                {
                    "ts_code": ["000001.SZ"],
                    "trade_date": [kwargs["trade_date"]],
                    "value": [1.0],
                }
            )

    fake = FakePro()
    monkeypatch.setattr(
        "ashare_research.data_sync.build_tushare_client",
        lambda _env: fake,
    )

    first = sync_tushare(
        tmp_path,
        date(2026, 7, 15),
        date(2026, 7, 16),
        request_interval_seconds=0,
        max_workers=4,
    )
    second = sync_tushare(
        tmp_path,
        date(2026, 7, 15),
        date(2026, 7, 16),
        request_interval_seconds=0,
        max_workers=4,
    )

    assert first.data_quality_passed
    assert first.written_partitions == 14
    assert second.written_partitions == 0
    assert second.skipped_partitions == 14
    membership = pl.read_parquet(
        tmp_path / "reference" / "sw_industry_membership.parquet"
    )
    assert membership.filter(pl.col("ts_code") == "000002.SZ").row(
        0,
        named=True,
    )["l1_code"] == "801780.SI"
    assert "nan" not in membership["l1_code"].str.to_lowercase().to_list()
