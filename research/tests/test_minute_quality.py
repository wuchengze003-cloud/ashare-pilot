"""Tests for minute_quality module: OHLC, duplicates, sessions, coverage."""

from __future__ import annotations

import json
from types import SimpleNamespace

import polars as pl

from ashare_research.cli import cmd_minute_health
from ashare_research.minute_data import _atomic_parquet, _partition_path, _valid_5min_times
from ashare_research.minute_quality import (
    COVERAGE_THRESHOLD_PCT,
    QUALITY_REPORT_VERSION,
    check_bar_count,
    check_daily_minute_gap,
    check_ohlc,
    check_required_values,
    check_time_monotonic,
    check_time_order,
    check_trading_session,
    check_volume_amount,
    run_minute_quality,
)


def _make_bars(ts_code: str, trade_date: str, n: int = 48, **overrides) -> pl.DataFrame:
    """Create synthetic bars with optional overrides for testing."""
    times = _valid_5min_times()[:n]
    rows = []
    for i, t in enumerate(times):
        row = {
            "ts_code": ts_code,
            "symbol": ts_code.split(".")[0],
            "trade_date": trade_date,
            "trade_time": f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:8]} {t}",
            "freq": "5min",
            "open": 10.0 + i * 0.01,
            "high": 10.0 + i * 0.01 + 0.05,
            "low": 10.0 + i * 0.01 - 0.02,
            "close": 10.0 + i * 0.01 + 0.02,
            "volume": 100000,
            "amount": 1000000.0,
            "source": "tushare_stk_mins",
            "realtime": False,
            "fetched_at": "2025-01-01T00:00:00+00:00",
        }
        row.update(overrides)
        rows.append(row)
    return pl.DataFrame(rows)


class TestOHLC:
    def test_valid_ohlc(self):
        df = _make_bars("000001.SZ", "20250101")
        assert check_ohlc(df) == 0

    def test_invalid_low_above_close(self):
        """low > close should be flagged."""
        df = _make_bars("000001.SZ", "20250101", n=5)
        # Make low > close for one bar
        df = df.with_columns(
            pl.when(pl.int_range(pl.len()) == 2)
            .then(pl.lit(11.0))
            .otherwise(pl.col("low"))
            .alias("low")
        )
        assert check_ohlc(df) >= 1

    def test_invalid_high_below_open(self):
        """high < open should be flagged."""
        df = _make_bars("000001.SZ", "20250101", n=5)
        df = df.with_columns(
            pl.when(pl.int_range(pl.len()) == 1)
            .then(pl.lit(9.0))
            .otherwise(pl.col("high"))
            .alias("high")
        )
        assert check_ohlc(df) >= 1

    def test_empty_df(self):
        assert check_ohlc(pl.DataFrame()) == 0


class TestVolumeAmount:
    def test_valid(self):
        df = _make_bars("000001.SZ", "20250101")
        neg_vol, neg_amt = check_volume_amount(df)
        assert neg_vol == 0
        assert neg_amt == 0

    def test_negative_volume(self):
        df = _make_bars("000001.SZ", "20250101", n=3)
        df = df.with_columns(
            pl.when(pl.int_range(pl.len()) == 0)
            .then(pl.lit(-100))
            .otherwise(pl.col("volume"))
            .alias("volume")
        )
        neg_vol, _ = check_volume_amount(df)
        assert neg_vol == 1

    def test_negative_amount(self):
        df = _make_bars("000001.SZ", "20250101", n=3)
        df = df.with_columns(
            pl.when(pl.int_range(pl.len()) == 1)
            .then(pl.lit(-5000.0))
            .otherwise(pl.col("amount"))
            .alias("amount")
        )
        _, neg_amt = check_volume_amount(df)
        assert neg_amt == 1


class TestRequiredValues:
    def test_nan_numeric_is_rejected(self):
        df = _make_bars("000001.SZ", "20250101", n=3).with_columns(
            pl.when(pl.int_range(pl.len()) == 1)
            .then(pl.lit(float("nan")))
            .otherwise(pl.col("close"))
            .alias("close")
        )

        invalid_numeric, missing_required, date_mismatch = check_required_values(
            df
        )

        assert invalid_numeric == 1
        assert missing_required == 0
        assert date_mismatch == 0

    def test_nonpositive_ohlc_is_rejected(self):
        df = _make_bars("000001.SZ", "20250101", n=3).with_columns(
            pl.when(pl.int_range(pl.len()) == 1)
            .then(pl.lit(0.0))
            .otherwise(pl.col("close"))
            .alias("close")
        )

        invalid_numeric, missing_required, date_mismatch = check_required_values(
            df
        )

        assert invalid_numeric == 1
        assert missing_required == 0
        assert date_mismatch == 0

    def test_trade_date_must_match_timestamp(self):
        df = _make_bars("000001.SZ", "20250101", n=3).with_columns(
            pl.when(pl.int_range(pl.len()) == 1)
            .then(pl.lit("2025-01-02 09:40:00"))
            .otherwise(pl.col("trade_time"))
            .alias("trade_time")
        )

        _, _, date_mismatch = check_required_values(df)

        assert date_mismatch == 1


class TestTimeMonotonic:
    def test_no_duplicates(self):
        df = _make_bars("000001.SZ", "20250101")
        assert check_time_monotonic(df) == 0

    def test_duplicate_time(self):
        """Duplicate trade_time should be detected."""
        df = _make_bars("000001.SZ", "20250101", n=5)
        # Duplicate the first row
        dup = df.row(0, named=True)
        df2 = pl.concat([df, pl.DataFrame([dup])])
        assert check_time_monotonic(df2) == 1

    def test_backwards_timestamp(self):
        df = _make_bars("000001.SZ", "20250101", n=5).sort(
            "trade_time", descending=True
        )
        assert check_time_order(df) > 0


class TestTradingSession:
    def test_valid_times(self):
        df = _make_bars("000001.SZ", "20250101")
        assert check_trading_session(df, "5min") == 0

    def test_lunch_break_bar(self):
        """Bar during lunch break (11:31-12:59) should be flagged."""
        df = _make_bars("000001.SZ", "20250101", n=3)
        # Replace one time with lunch break time
        df = df.with_columns(
            pl.when(pl.int_range(pl.len()) == 1)
            .then(pl.lit("2025-01-01 12:30:00"))
            .otherwise(pl.col("trade_time"))
            .alias("trade_time")
        )
        assert check_trading_session(df, "5min") == 1


class TestBarCount:
    def test_full_day_no_warning(self):
        df = _make_bars("000001.SZ", "20250101", n=48)
        warnings = check_bar_count(df, "5min", 48)
        assert len(warnings) == 0

    def test_low_bar_count_warning(self):
        """Significantly fewer bars than expected triggers warning."""
        df = _make_bars("000001.SZ", "20250101", n=10)
        warnings = check_bar_count(df, "5min", 48)
        assert len(warnings) == 1
        assert warnings[0]["bar_count"] == 10


class TestDailyMinuteGap:
    def test_no_gap(self):
        df = _make_bars("000001.SZ", "20250101")
        missing = check_daily_minute_gap(df, {"20250101"}, set(), "000001.SZ")
        assert missing == []

    def test_gap_detected(self):
        """Daily has volume but minute empty → missing."""
        df = _make_bars("000001.SZ", "20250101")
        missing = check_daily_minute_gap(df, {"20250101", "20250102"}, set(), "000001.SZ")
        assert "20250102" in missing

    def test_suspended_not_flagged(self):
        """Suspended days should not be flagged as missing."""
        df = _make_bars("000001.SZ", "20250101")
        missing = check_daily_minute_gap(
            df, {"20250101", "20250102"}, {"20250102"}, "000001.SZ"
        )
        assert missing == []


class TestCoverageThreshold:
    def test_calendar_is_required_even_when_data_exists(self, tmp_path):
        df = _make_bars("000001.SZ", "20250101")
        target = _partition_path(
            tmp_path, "5min", "000001.SZ", 2025, 1
        )
        _atomic_parquet(df, target)

        report = run_minute_quality(
            tmp_path, "2025-01-01", "2025-01-01", "5min"
        )

        assert not report.passed
        assert report.coverage_pct == 0.0
        assert any("trading_dates not provided" in item for item in report.failures)
        assert report.to_dict()["quality_version"] == QUALITY_REPORT_VERSION

    def test_raw_duplicate_is_not_hidden_by_loader_dedup(self, tmp_path):
        df = _make_bars("000001.SZ", "20250101")
        duplicate = pl.DataFrame([df.row(0, named=True)])
        target = _partition_path(
            tmp_path, "5min", "000001.SZ", 2025, 1
        )
        _atomic_parquet(pl.concat([df, duplicate]), target)

        report = run_minute_quality(
            tmp_path,
            "2025-01-01",
            "2025-01-01",
            "5min",
            trading_dates=["20250101"],
            expected_symbols=["000001.SZ"],
        )

        assert not report.passed
        assert any(issue.rule == "duplicate_time" for issue in report.issues)
        assert report.to_dict()["duplicate_keys"] == 1

    def test_below_95_fails(self, tmp_path):
        """Coverage < 95% should cause event study to fail closed."""
        # Write data for only 1 out of 10 expected trading days
        df = _make_bars("000001.SZ", "20250101")
        target = _partition_path(tmp_path, "5min", "000001.SZ", 2025, 1)
        _atomic_parquet(df, target)

        trading_dates = [f"202501{d:02d}" for d in range(1, 11)]
        report = run_minute_quality(
            tmp_path, "2025-01-01", "2025-01-10", "5min",
            trading_dates=trading_dates,
        )
        assert report.coverage_pct < COVERAGE_THRESHOLD_PCT
        assert not report.passed
        assert any("coverage" in f for f in report.failures)

    def test_above_95_passes(self, tmp_path):
        """Coverage >= 95% should pass."""
        # Write data for all expected dates
        for d in range(1, 11):
            df = _make_bars("000001.SZ", f"202501{d:02d}")
            target = _partition_path(tmp_path, "5min", "000001.SZ", 2025, 1)
            if target.exists():
                old = pl.read_parquet(target)
                combined = pl.concat([old, df]).unique(
                    subset=["ts_code", "trade_time", "freq"], keep="last"
                ).sort("trade_time")
                _atomic_parquet(combined, target)
            else:
                _atomic_parquet(df, target)

        trading_dates = [f"202501{d:02d}" for d in range(1, 11)]
        report = run_minute_quality(
            tmp_path, "2025-01-01", "2025-01-10", "5min",
            trading_dates=trading_dates,
        )
        assert report.coverage_pct >= COVERAGE_THRESHOLD_PCT
        assert report.passed
        assert report.symbols_checked == 1
        assert report.symbols_with_data == 1
        assert report.symbol_ranges["000001.SZ"]["first_time"].endswith(
            "09:35:00"
        )
        assert report.symbol_ranges["000001.SZ"]["last_time"].endswith(
            "15:00:00"
        )

    def test_expected_universe_ignores_stale_probe_symbols(self, tmp_path):
        current = _make_bars("300308.SZ", "20250101")
        stale = _make_bars("000001.SZ", "20250101").with_columns(
            pl.lit(-1.0).alias("amount")
        )
        _atomic_parquet(
            current,
            _partition_path(tmp_path, "5min", "300308.SZ", 2025, 1),
        )
        _atomic_parquet(
            stale,
            _partition_path(tmp_path, "5min", "000001.SZ", 2025, 1),
        )

        report = run_minute_quality(
            tmp_path,
            "2025-01-01",
            "2025-01-01",
            "5min",
            trading_dates=["20250101"],
            daily_volume_map={"300308.SZ": {"20250101"}},
            expected_symbols=["300308.SZ"],
        )

        assert report.passed
        assert report.symbols_checked == 1
        assert report.symbols_with_data == 1
        assert set(report.per_symbol_coverage) == {"300308.SZ"}
        assert set(report.symbol_ranges) == {"300308.SZ"}

    def test_invalid_numeric_cannot_pass_health_gate(self, tmp_path):
        df = _make_bars("000001.SZ", "20250101").with_columns(
            pl.when(pl.int_range(pl.len()) == 2)
            .then(pl.lit(float("nan")))
            .otherwise(pl.col("amount"))
            .alias("amount")
        )
        target = _partition_path(
            tmp_path, "5min", "000001.SZ", 2025, 1
        )
        _atomic_parquet(df, target)

        report = run_minute_quality(
            tmp_path,
            "2025-01-01",
            "2025-01-01",
            "5min",
            trading_dates=["20250101"],
            expected_symbols=["000001.SZ"],
        )

        assert not report.passed
        assert report.to_dict()["invalid_numeric"] == 1

    def test_partition_symbol_mismatch_cannot_pass(self, tmp_path):
        df = _make_bars("300308.SZ", "20250101")
        target = _partition_path(
            tmp_path, "5min", "000001.SZ", 2025, 1
        )
        _atomic_parquet(df, target)

        report = run_minute_quality(
            tmp_path,
            "2025-01-01",
            "2025-01-01",
            "5min",
            trading_dates=["20250101"],
            expected_symbols=["000001.SZ"],
        )

        assert not report.passed
        assert any(
            issue.rule == "partition_symbol_mismatch"
            for issue in report.issues
        )

    def test_wrong_frequency_or_realtime_rows_cannot_pass(self, tmp_path):
        df = _make_bars("000001.SZ", "20250101").with_columns(
            pl.lit("1min").alias("freq"),
            pl.lit(True).alias("realtime"),
        )
        target = _partition_path(
            tmp_path, "5min", "000001.SZ", 2025, 1
        )
        _atomic_parquet(df, target)

        report = run_minute_quality(
            tmp_path,
            "2025-01-01",
            "2025-01-01",
            "5min",
            trading_dates=["20250101"],
            expected_symbols=["000001.SZ"],
        )

        assert not report.passed
        assert {
            issue.rule for issue in report.issues
        } >= {"invalid_frequency", "invalid_realtime"}

    def test_suspended_zero_turnover_day_is_excluded_not_failed(
        self, tmp_path
    ):
        bars = _make_bars(
            "000001.SZ",
            "20250101",
            volume=0,
            amount=0.0,
        )
        target = _partition_path(
            tmp_path, "5min", "000001.SZ", 2025, 1
        )
        _atomic_parquet(bars, target)

        report = run_minute_quality(
            tmp_path,
            "2025-01-01",
            "2025-01-01",
            "5min",
            trading_dates=["20250101"],
            daily_volume_map={"000001.SZ": {"20250101"}},
            suspended_map={"000001.SZ": {"20250101"}},
            expected_symbols=["000001.SZ"],
        )

        assert report.passed
        assert report.total_rows == 48
        assert report.total_bars == 0
        assert report.zero_turnover_symbol_days == 1
        assert report.excluded_zero_turnover_bars == 48
        assert report.unexpected_zero_turnover_symbol_days == 0
        assert any(
            issue.rule == "zero_turnover_non_trading_day"
            and issue.severity == "warning"
            for issue in report.issues
        )

    def test_zero_turnover_day_without_positive_daily_row_is_excluded(
        self, tmp_path
    ):
        bars = _make_bars(
            "688143.SH",
            "20250101",
            volume=0,
            amount=0.0,
        )
        target = _partition_path(
            tmp_path, "5min", "688143.SH", 2025, 1
        )
        _atomic_parquet(bars, target)

        report = run_minute_quality(
            tmp_path,
            "2025-01-01",
            "2025-01-01",
            "5min",
            trading_dates=["20250101"],
            daily_volume_map={},
            expected_symbols=["688143.SH"],
        )

        assert report.passed
        assert report.coverage_pct == 100.0
        assert report.total_bars == 0
        assert report.unexpected_zero_turnover_symbol_days == 0

    def test_zero_turnover_day_with_positive_daily_volume_fails(
        self, tmp_path
    ):
        bars = _make_bars(
            "000001.SZ",
            "20250101",
            volume=0,
            amount=0.0,
        )
        target = _partition_path(
            tmp_path, "5min", "000001.SZ", 2025, 1
        )
        _atomic_parquet(bars, target)

        report = run_minute_quality(
            tmp_path,
            "2025-01-01",
            "2025-01-01",
            "5min",
            trading_dates=["20250101"],
            daily_volume_map={"000001.SZ": {"20250101"}},
            expected_symbols=["000001.SZ"],
        )

        assert not report.passed
        assert report.coverage_pct == 0.0
        assert report.total_rows == 48
        assert report.total_bars == 0
        assert report.unexpected_zero_turnover_symbol_days == 1
        assert any(
            issue.rule == "unexpected_zero_turnover_day"
            and issue.severity == "error"
            for issue in report.issues
        )


def test_minute_health_fails_closed_when_universe_is_missing(
    tmp_path, capsys
):
    args = SimpleNamespace(
        runtime=str(tmp_path),
        start="2025-01-01",
        end="2025-01-01",
        freq="5min",
        universe=str(tmp_path / "missing-universe.json"),
    )

    assert cmd_minute_health(args) == 1
    capsys.readouterr()
    coverage = json.loads(
        (tmp_path / "minute" / "meta" / "coverage.json").read_text("utf-8")
    )
    assert coverage["passed"] is False
    assert any(
        "universe is missing" in failure
        for failure in coverage["failures"]
    )


def test_minute_health_excludes_future_universe_members(
    tmp_path, capsys
):
    universe_path = tmp_path / "universe.json"
    universe_path.write_text(
        json.dumps(
            {
                "entries": [
                    {
                        "symbol": "000001",
                        "pool_tier": "core",
                    },
                    {
                        "symbol": "688825",
                        "pool_tier": "core",
                        "strategy_from": "2026-07-27",
                    },
                ]
            }
        ),
        "utf-8",
    )
    args = SimpleNamespace(
        runtime=str(tmp_path),
        start="2025-01-01",
        end="2025-01-31",
        freq="5min",
        universe=str(universe_path),
    )

    assert cmd_minute_health(args) == 1
    capsys.readouterr()
    coverage = json.loads(
        (tmp_path / "minute" / "meta" / "coverage.json").read_text("utf-8")
    )
    assert coverage["expected_symbols"] == ["000001.SZ"]
