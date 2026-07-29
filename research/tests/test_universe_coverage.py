"""Tests for the historical universe coverage audit (merge blocker 2)."""

from datetime import date, timedelta

import polars as pl

from ashare_research.universe_coverage import build_historical_universe_coverage

START = date(2024, 1, 2)
DAYS = 20


def _day(index: int) -> str:
    return (START + timedelta(days=index)).strftime("%Y%m%d")


def _write_fixture(
    root,
    *,
    daily_rows,
    suspended_rows=(),
    membership_rows=None,
    stock_rows=None,
):
    reference = root / "reference"
    reference.mkdir(parents=True)
    if stock_rows is None:
        stock_rows = [
            {
                "ts_code": "000001.SZ",
                "name": "A",
                "list_status": "L",
                "list_date": "20200101",
                "delist_date": None,
            },
            {
                "ts_code": "600000.SH",
                "name": "B",
                "list_status": "L",
                "list_date": "20200101",
                "delist_date": None,
            },
        ]
    pl.DataFrame(stock_rows).write_parquet(reference / "stock_basic.parquet")
    if membership_rows is None:
        membership_rows = [
            {
                "instrument": "sz000001",
                "member_start": START,
                "member_end": START + timedelta(days=DAYS - 1),
            },
            {
                "instrument": "sh600000",
                "member_start": START,
                "member_end": START + timedelta(days=DAYS - 1),
            },
        ]
    pl.DataFrame(membership_rows).write_parquet(reference / "csi800_membership.parquet")
    pl.DataFrame(
        {
            "cal_date": [_day(i) for i in range(DAYS)],
            "is_open": [1] * DAYS,
        }
    ).write_parquet(reference / "trade_cal.parquet")

    daily_dir = root / "raw" / "daily" / "trade_date=20240102"
    daily_dir.mkdir(parents=True)
    pl.DataFrame(
        daily_rows,
        schema={"ts_code": pl.String, "trade_date": pl.String},
    ).write_parquet(daily_dir / "part.parquet")
    if suspended_rows:
        sus_dir = root / "raw" / "suspend_d" / "trade_date=20240102"
        sus_dir.mkdir(parents=True)
        pl.DataFrame(
            suspended_rows,
            schema={"ts_code": pl.String, "trade_date": pl.String},
        ).write_parquet(sus_dir / "part.parquet")


def _full_daily(symbols=("000001.SZ", "600000.SH")):
    return [
        {"ts_code": symbol, "trade_date": _day(i)}
        for symbol in symbols
        for i in range(DAYS)
    ]


def test_full_coverage_passes(tmp_path):
    _write_fixture(tmp_path, daily_rows=_full_daily())
    report = build_historical_universe_coverage(
        tmp_path, expected_member_count=2
    )
    assert report["passed"] is True
    assert report["coverage"]["missing_member_days"] == 0
    assert report["point_in_time_member_count"]["distribution"] == {"2": 1}


def test_missing_bars_without_suspension_fails(tmp_path):
    rows = _full_daily()
    # Stock A loses half of its bars with no suspension records.
    rows = [r for r in rows if not (r["ts_code"] == "000001.SZ" and r["trade_date"] >= _day(10))]
    _write_fixture(tmp_path, daily_rows=rows)
    report = build_historical_universe_coverage(tmp_path, expected_member_count=2)
    assert report["passed"] is False
    assert report["silently_skipped_count"] == 0  # A still has 10 bars
    assert any(g["symbol"] == "sz000001" for g in report["gap_members"])
    assert any("coverage threshold" in r for r in report["fail_conditions"]["fail_reasons"])


def test_zero_bars_listed_member_is_silent_skip_and_fails(tmp_path):
    rows = [r for r in _full_daily() if r["ts_code"] == "600000.SH"]
    _write_fixture(tmp_path, daily_rows=rows)
    report = build_historical_universe_coverage(tmp_path, expected_member_count=2)
    assert report["passed"] is False
    assert report["silently_skipped_count"] == 1
    assert report["silently_skipped"][0]["symbol"] == "sz000001"


def test_fully_suspended_member_is_not_a_gap(tmp_path):
    rows = [r for r in _full_daily() if r["ts_code"] == "600000.SH"]
    suspended = [
        {"ts_code": "000001.SZ", "trade_date": _day(i)} for i in range(DAYS)
    ]
    _write_fixture(tmp_path, daily_rows=rows, suspended_rows=suspended)
    report = build_historical_universe_coverage(tmp_path, expected_member_count=2)
    assert report["passed"] is True
    assert report["silently_skipped_count"] == 0
    assert report["coverage"]["missing_member_days"] == 0


def test_delisted_member_expectation_clipped_at_delist_date(tmp_path):
    stock_rows = [
        {
            "ts_code": "000001.SZ",
            "name": "A",
            "list_status": "D",
            "list_date": "20200101",
            "delist_date": _day(9),
        },
        {
            "ts_code": "600000.SH",
            "name": "B",
            "list_status": "L",
            "list_date": "20200101",
            "delist_date": None,
        },
    ]
    rows = [
        {"ts_code": "000001.SZ", "trade_date": _day(i)} for i in range(10)
    ] + [{"ts_code": "600000.SH", "trade_date": _day(i)} for i in range(DAYS)]
    _write_fixture(tmp_path, daily_rows=rows, stock_rows=stock_rows)
    report = build_historical_universe_coverage(tmp_path, expected_member_count=2)
    assert report["passed"] is True
    assert report["coverage"]["missing_member_days"] == 0


def test_member_count_anomaly_detected(tmp_path):
    membership_rows = [
        {
            "instrument": "sz000001",
            "member_start": START,
            "member_end": START + timedelta(days=DAYS - 1),
        },
        {
            # B only joins on day 5 → days 0-4 have count 1 != 2.
            "instrument": "sh600000",
            "member_start": START + timedelta(days=5),
            "member_end": START + timedelta(days=DAYS - 1),
        },
    ]
    _write_fixture(
        tmp_path, daily_rows=_full_daily(), membership_rows=membership_rows
    )
    report = build_historical_universe_coverage(tmp_path, expected_member_count=2)
    assert report["passed"] is False
    assert report["point_in_time_member_count"]["abnormal_periods"]
