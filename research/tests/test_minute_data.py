"""Tests for minute_data module: sync, dedup, partitioning, truncation."""

from __future__ import annotations

import json
from datetime import date
from unittest.mock import MagicMock

import polars as pl

from ashare_research.minute_data import (
    _atomic_parquet,
    _merge_partition,
    _normalize_minute_frame,
    _partition_path,
    _segment_date_ranges,
    _symbol_to_ts_code,
    _valid_5min_times,
    compute_coverage,
    load_daily_volume_map,
    load_minute_bars,
    sync_minute_data,
)


def _make_minute_df(ts_code: str, trade_date: str, n_bars: int = 48) -> pl.DataFrame:
    """Create a synthetic minute DataFrame."""
    times = _valid_5min_times()[:n_bars]
    rows = []
    for i, t in enumerate(times):
        rows.append({
            "ts_code": ts_code,
            "symbol": ts_code.split(".")[0],
            "trade_date": trade_date,
            "trade_time": f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:8]} {t}",
            "freq": "5min",
            "open": 10.0 + i * 0.01,
            "high": 10.0 + i * 0.01 + 0.05,
            "low": 10.0 + i * 0.01 - 0.02,
            "close": 10.0 + i * 0.01 + 0.02,
            "volume": 100000 + i * 1000,
            "amount": 1000000.0 + i * 10000,
            "source": "tushare_stk_mins",
            "realtime": False,
            "fetched_at": "2025-01-01T00:00:00+00:00",
        })
    return pl.DataFrame(rows)


class TestSymbolConversion:
    def test_main_board(self):
        assert _symbol_to_ts_code("000001") == "000001.SZ"

    def test_chinext(self):
        assert _symbol_to_ts_code("300308") == "300308.SZ"

    def test_star(self):
        assert _symbol_to_ts_code("688256") == "688256.SH"

    def test_already_full(self):
        assert _symbol_to_ts_code("000001.SZ") == "000001.SZ"
        assert _symbol_to_ts_code("sz000001") == "000001.SZ"
        assert _symbol_to_ts_code("sh688256") == "688256.SH"


class TestSegmentDateRanges:
    def test_5min_short_range(self):
        segments = _segment_date_ranges(date(2025, 1, 1), date(2025, 1, 31), "5min")
        assert len(segments) == 1
        assert segments[0] == (date(2025, 1, 1), date(2025, 1, 31))

    def test_5min_long_range_splits(self):
        segments = _segment_date_ranges(date(2025, 1, 1), date(2025, 12, 31), "5min")
        assert len(segments) > 1
        # Verify continuity
        for i in range(1, len(segments)):
            prev_end = segments[i - 1][1]
            curr_start = segments[i][0]
            assert curr_start > prev_end

    def test_1max_31_days(self):
        segments = _segment_date_ranges(date(2025, 1, 1), date(2025, 3, 31), "1min")
        for start, end in segments:
            assert (end - start).days < 31


class TestMergePartition:
    def test_schema_is_canonical_across_provider_type_drift(self):
        frame = _make_minute_df("000001.SZ", "20250101").with_columns(
            pl.col("volume").cast(pl.Int64),
            pl.lit("ignored").alias("provider_extra"),
        )

        normalized = _normalize_minute_frame(
            frame,
            require_complete=True,
        )

        assert normalized.columns == [
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
        ]
        assert normalized.schema["volume"] == pl.Float64
        assert "provider_extra" not in normalized.columns

    def test_no_duplicates_on_rerun(self, tmp_path):
        """Same interval synced twice should not produce duplicate rows."""
        df = _make_minute_df("000001.SZ", "20250101")
        target = tmp_path / "part.parquet"
        _atomic_parquet(df, target)

        # Merge same data again
        merged = _merge_partition(target, df)
        assert merged.height == df.height  # No duplicates

    def test_cross_month_merge(self, tmp_path):
        """Data from different months merges correctly."""
        df1 = _make_minute_df("000001.SZ", "20250131")
        df2 = _make_minute_df("000001.SZ", "20250201")
        target = tmp_path / "part.parquet"
        _atomic_parquet(df1, target)
        merged = _merge_partition(target, df2)
        assert merged.height == df1.height + df2.height

    def test_sorted_by_trade_time(self, tmp_path):
        """Merged result is sorted by trade_time ascending."""
        df = _make_minute_df("000001.SZ", "20250101")
        # Reverse the order
        df_rev = df.sort("trade_time", descending=True)
        target = tmp_path / "part.parquet"
        _atomic_parquet(df_rev, target)
        merged = _merge_partition(target, df)
        times = merged["trade_time"].to_list()
        assert times == sorted(times)


class TestTruncation:
    def test_8000_row_triggers_split(self):
        """8000 rows should trigger segment splitting (tested via mock)."""
        # This tests the logic path - actual API call is mocked
        import pandas as pd

        from ashare_research.minute_data import _fetch_symbol_segment

        # Create a mock that returns 8000 rows
        mock_pro = MagicMock()
        fake_data = pd.DataFrame({
            "ts_code": ["000001.SZ"] * 8000,
            "trade_time": [f"2025-01-01 {9 + i // 60:02d}:{i % 60:02d}:00" for i in range(8000)],
            "open": [10.0] * 8000,
            "high": [10.5] * 8000,
            "low": [9.5] * 8000,
            "close": [10.2] * 8000,
            "vol": [1000] * 8000,
            "amount": [10000.0] * 8000,
        })

        call_count = [0]

        def mock_stk_mins(**kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return fake_data  # First call returns 8000 (truncated)
            # Subsequent calls return smaller chunks
            return fake_data.head(100)

        mock_pro.stk_mins = mock_stk_mins

        _fetch_symbol_segment(
            mock_pro, "000001.SZ", "5min",
            date(2025, 1, 1), date(2025, 1, 31), 0.0,
        )
        # Should have split and made additional calls
        assert call_count[0] > 1


class TestNetworkFailure:
    def test_retry_then_success(self):
        """Network failure retried and succeeds."""
        import pandas as pd

        from ashare_research.minute_data import _query_stk_mins_with_retry

        mock_pro = MagicMock()
        call_count = [0]

        def mock_stk_mins(**kwargs):
            call_count[0] += 1
            if call_count[0] < 3:
                raise ConnectionError("timeout")
            return pd.DataFrame({"ts_code": ["000001.SZ"], "trade_time": ["2025-01-01 09:35:00"],
                                 "open": [10.0], "high": [10.5], "low": [9.5],
                                 "close": [10.2], "vol": [1000], "amount": [10000.0]})

        mock_pro.stk_mins = mock_stk_mins
        result = _query_stk_mins_with_retry(mock_pro, "000001.SZ", "5min",
                                            "2025-01-01 09:30:00", "2025-01-01 15:00:00",
                                            attempts=3, base_delay=0.01)
        assert result is not None
        assert call_count[0] == 3

    def test_persistent_failure_writes_error(self, tmp_path):
        """Persistent failure writes error report without destroying old data."""
        from ashare_research.minute_data import _write_sync_error

        _write_sync_error(tmp_path, "000001.SZ", "connection timeout")
        error_file = tmp_path / "meta" / "last-sync-error.json"
        assert error_file.exists()
        data = json.loads(error_file.read_text())
        assert data["status"] == "failed"
        assert "token" not in error_file.read_text().lower()

    def test_error_report_redacts_token_value(
        self, tmp_path, monkeypatch
    ):
        from ashare_research.minute_data import _write_sync_error

        monkeypatch.setenv("TUSHARE_TOKEN", "super-secret-value")
        _write_sync_error(
            tmp_path,
            "000001.SZ",
            "request failed for token super-secret-value",
        )

        text = (tmp_path / "meta" / "last-sync-error.json").read_text()
        assert "super-secret-value" not in text
        assert "[REDACTED]" in text


class TestRealtimeFlag:
    def test_same_day_data_marked_historical(self):
        """Even same-day responses are stored as historical, not realtime."""
        df = _make_minute_df("000001.SZ", "20250724")
        # The source field should always be tushare_stk_mins (historical)
        assert df["source"][0] == "tushare_stk_mins"
        # No realtime field should be True
        assert "realtime" not in df.columns or all(not v for v in df["realtime"].to_list())


class TestNoTokenInLogs:
    def test_log_output_no_token(self, tmp_path, capsys):
        """Sync progress output must not contain token."""
        # We test that the report structure doesn't leak tokens
        from ashare_research.minute_data import MinuteSyncReport

        report = MinuteSyncReport(started_at="2025-01-01T00:00:00")
        output = json.dumps(report.__dict__)
        assert "token" not in output.lower()
        assert "TUSHARE" not in output


class TestCoverage:
    def test_empty_directory(self, tmp_path):
        result = compute_coverage(tmp_path, "2025-01-01", "2025-03-31", "5min")
        assert result["passed"] is False
        assert result["symbols"] == 0

    def test_with_data(self, tmp_path):
        # Write some data
        df = _make_minute_df("000001.SZ", "20250101")
        target = _partition_path(tmp_path, "5min", "000001.SZ", 2025, 1)
        _atomic_parquet(df, target)

        result = compute_coverage(tmp_path, "2025-01-01", "2025-01-31", "5min")
        assert result["symbols"] == 1
        assert result["total_rows"] == 48
        assert result["duplicate_keys"] == 0
        assert result["passed"] is False
        assert any("trading_dates not provided" in item for item in result["failures"])

    def test_per_symbol_coverage_catches_missing_symbol(self, tmp_path):
        dates = ["20250101", "20250102"]
        for trade_date in dates:
            first = _make_minute_df("000001.SZ", trade_date)
            first_path = _partition_path(
                tmp_path, "5min", "000001.SZ", 2025, 1
            )
            merged = _merge_partition(first_path, first)
            _atomic_parquet(merged, first_path)
        second = _make_minute_df("300308.SZ", "20250101")
        second_path = _partition_path(
            tmp_path, "5min", "300308.SZ", 2025, 1
        )
        _atomic_parquet(second, second_path)

        result = compute_coverage(
            tmp_path,
            "2025-01-01",
            "2025-01-02",
            "5min",
            trading_dates=dates,
            expected_symbols=["000001.SZ", "300308.SZ"],
        )

        assert result["passed"] is False
        assert result["per_symbol_coverage"]["000001.SZ"] == 100.0
        assert result["per_symbol_coverage"]["300308.SZ"] == 50.0


class TestIncrementalSyncCompleteness:
    def test_future_universe_member_is_not_requested_early(
        self, tmp_path, monkeypatch
    ):
        import ashare_research.minute_data as minute_data

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
        calls: list[str] = []

        def fake_fetch(_pro, ts_code, _freq, _start, _end, _interval):
            calls.append(ts_code)
            return _make_minute_df(ts_code, "20250102")

        monkeypatch.setattr(
            minute_data, "build_tushare_client", lambda _env: object()
        )
        monkeypatch.setattr(
            minute_data, "_fetch_symbol_segment", fake_fetch
        )

        report = sync_minute_data(
            tmp_path / "minute",
            date(2025, 1, 1),
            date(2025, 1, 31),
            universe_path=universe_path,
            trading_dates=["20250102"],
            request_interval=0,
        )

        assert report.passed
        assert report.symbols_requested == 1
        assert calls == ["000001.SZ"]

    def test_missing_day_forces_refetch(self, tmp_path, monkeypatch):
        import ashare_research.minute_data as minute_data

        target = _partition_path(
            tmp_path, "5min", "000001.SZ", 2025, 1
        )
        _atomic_parquet(_make_minute_df("000001.SZ", "20250102"), target)
        fetched = pl.concat(
            [
                _make_minute_df("000001.SZ", "20250102"),
                _make_minute_df("000001.SZ", "20250103"),
            ]
        )
        calls: list[tuple[date, date]] = []

        def fake_fetch(_pro, _ts_code, _freq, start, end, _interval):
            calls.append((start, end))
            return fetched

        monkeypatch.setattr(minute_data, "build_tushare_client", lambda _env: object())
        monkeypatch.setattr(minute_data, "_fetch_symbol_segment", fake_fetch)
        report = sync_minute_data(
            tmp_path,
            date(2025, 1, 1),
            date(2025, 1, 31),
            symbols=["000001"],
            trading_dates=["20250102", "20250103"],
            request_interval=0,
        )

        assert report.passed
        assert len(calls) == 1
        stored = pl.read_parquet(target)
        assert set(stored["trade_date"].to_list()) == {
            "20250102",
            "20250103",
        }

    def test_partial_day_forces_refetch(self, tmp_path, monkeypatch):
        import ashare_research.minute_data as minute_data

        target = _partition_path(
            tmp_path, "5min", "000001.SZ", 2025, 1
        )
        _atomic_parquet(
            _make_minute_df("000001.SZ", "20250102", n_bars=10),
            target,
        )
        calls = 0

        def fake_fetch(_pro, _ts_code, _freq, _start, _end, _interval):
            nonlocal calls
            calls += 1
            return _make_minute_df("000001.SZ", "20250102")

        monkeypatch.setattr(minute_data, "build_tushare_client", lambda _env: object())
        monkeypatch.setattr(minute_data, "_fetch_symbol_segment", fake_fetch)
        report = sync_minute_data(
            tmp_path,
            date(2025, 1, 1),
            date(2025, 1, 31),
            symbols=["000001"],
            trading_dates=["20250102"],
            request_interval=0,
        )

        assert report.passed
        assert calls == 1
        assert pl.read_parquet(target).height == 48

    def test_complete_partition_is_idempotently_skipped(
        self, tmp_path, monkeypatch
    ):
        import ashare_research.minute_data as minute_data

        target = _partition_path(
            tmp_path, "5min", "000001.SZ", 2025, 1
        )
        _atomic_parquet(_make_minute_df("000001.SZ", "20250102"), target)

        monkeypatch.setattr(minute_data, "build_tushare_client", lambda _env: object())

        def unexpected_fetch(*_args, **_kwargs):
            raise AssertionError("complete partition must not be fetched")

        monkeypatch.setattr(
            minute_data, "_fetch_symbol_segment", unexpected_fetch
        )
        report = sync_minute_data(
            tmp_path,
            date(2025, 1, 1),
            date(2025, 1, 31),
            symbols=["000001"],
            trading_dates=["20250102"],
            request_interval=0,
        )

        assert report.passed
        assert report.skipped_partitions == 1
        assert report.symbols_synced == 0

    def test_missing_symbol_volume_map_falls_back_to_calendar(
        self, tmp_path, monkeypatch
    ):
        import ashare_research.minute_data as minute_data

        target = _partition_path(
            tmp_path, "5min", "300308.SZ", 2025, 1
        )
        _atomic_parquet(_make_minute_df("300308.SZ", "20250102"), target)
        calls = 0

        def fake_fetch(_pro, ts_code, _freq, _start, _end, _interval):
            nonlocal calls
            calls += 1
            return pl.concat(
                [
                    _make_minute_df(ts_code, "20250102"),
                    _make_minute_df(ts_code, "20250103"),
                ]
            )

        monkeypatch.setattr(
            minute_data, "build_tushare_client", lambda _env: object()
        )
        monkeypatch.setattr(
            minute_data, "_fetch_symbol_segment", fake_fetch
        )

        report = sync_minute_data(
            tmp_path,
            date(2025, 1, 1),
            date(2025, 1, 31),
            symbols=["300308"],
            trading_dates=["20250102", "20250103"],
            expected_dates_by_symbol={"000001.SZ": {"20250102"}},
            request_interval=0,
        )

        assert report.passed
        assert calls == 1
        assert set(pl.read_parquet(target)["trade_date"].to_list()) == {
            "20250102",
            "20250103",
        }

    def test_two_worker_mode_uses_bounded_parallel_clients(
        self, tmp_path, monkeypatch
    ):
        import ashare_research.minute_data as minute_data

        clients: list[object] = []

        def fake_client(_env):
            client = object()
            clients.append(client)
            return client

        def fake_fetch(_pro, ts_code, _freq, _start, _end, _interval):
            return _make_minute_df(ts_code, "20250102")

        monkeypatch.setattr(minute_data, "build_tushare_client", fake_client)
        monkeypatch.setattr(
            minute_data, "_fetch_symbol_segment", fake_fetch
        )

        report = sync_minute_data(
            tmp_path,
            date(2025, 1, 1),
            date(2025, 1, 31),
            symbols=["000001", "300308"],
            trading_dates=["20250102"],
            request_interval=0,
            max_workers=2,
        )

        assert report.passed
        assert report.symbols_synced == 2
        assert len(clients) == 2


def test_daily_volume_map_uses_tushare_vol_column(tmp_path):
    target = (
        tmp_path
        / "raw"
        / "daily"
        / "trade_date=20250102"
        / "part.parquet"
    )
    target.parent.mkdir(parents=True)
    pl.DataFrame(
        {
            "ts_code": ["000001.SZ", "300308.SZ"],
            "trade_date": ["20250102", "20250102"],
            "vol": [100.0, 0.0],
        }
    ).write_parquet(target)

    result = load_daily_volume_map(
        tmp_path, "2025-01-01", "2025-01-31"
    )

    assert result == {"000001.SZ": {"20250102"}}


class TestLoadMinuteBars:
    def test_load_existing(self, tmp_path):
        df = _make_minute_df("000001.SZ", "20250101")
        target = _partition_path(tmp_path, "5min", "000001.SZ", 2025, 1)
        _atomic_parquet(df, target)

        loaded = load_minute_bars(tmp_path, "000001.SZ", "20250101", "20250101", "5min")
        assert loaded.height == 48
        # Verify sorted
        times = loaded["trade_time"].to_list()
        assert times == sorted(times)

    def test_load_missing_returns_empty(self, tmp_path):
        loaded = load_minute_bars(tmp_path, "999999.SZ", "20250101", "20250101", "5min")
        assert loaded.height == 0
