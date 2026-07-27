"""End-to-end pipeline test with synthetic data.

Tests the full development → validation → lock → frozen pipeline
using synthetic daily and minute data.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import polars as pl
import pytest

from ashare_research.minute_data import _valid_5min_times
from ashare_research.minute_quality import QUALITY_REPORT_VERSION
from ashare_research.rebound_report import (
    _find_completed_frozen_run,
    _previous_close_on_next_day_basis,
    create_config_lock,
    run_rebound_study,
    verify_config_lock,
)
from ashare_research.rebound_study import ReboundEvent, hash_config


def _make_synthetic_daily(ts_code: str, trade_date: str, base_price: float = 10.0) -> pl.DataFrame:
    """Create synthetic daily bar."""
    return pl.DataFrame([{
        "ts_code": ts_code,
        "symbol": ts_code.split(".")[0],
        "trade_date": trade_date,
        "open": base_price,
        "high": base_price * 1.02,
        "low": base_price * 0.98,
        "close": base_price * 1.01,
        "vol": 10000000,
        "amount": base_price * 10000000,
        "is_st": False,
        "suspended": False,
        "adj_factor": 1.0,
        "up_limit": base_price * 1.2,
        "down_limit": base_price * 0.8,
    }])


def _make_synthetic_minute(ts_code: str, trade_date: str, base_price: float = 10.0) -> pl.DataFrame:
    """Create synthetic 5min bars for a full trading day."""
    times = _valid_5min_times()
    rows = []
    for i, t in enumerate(times):
        price = base_price + i * 0.01
        vol = 1000000
        rows.append({
            "ts_code": ts_code,
            "symbol": ts_code.split(".")[0],
            "trade_date": trade_date,
            "trade_time": f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:8]} {t}",
            "freq": "5min",
            "open": price,
            "high": price + 0.05,
            "low": price - 0.02,
            "close": price + 0.02,
            "volume": vol,
            "amount": price * vol,
            "source": "tushare_stk_mins",
            "fetched_at": "2025-01-01T00:00:00+00:00",
        })
    return pl.DataFrame(rows)


def _write_daily_partition(data_root: Path, trade_date: str, df: pl.DataFrame):
    """Write daily data to partitioned parquet."""
    partition_dir = data_root / "raw" / "daily" / f"trade_date={trade_date}"
    partition_dir.mkdir(parents=True, exist_ok=True)
    target = partition_dir / "part.parquet"
    if target.exists():
        df = pl.concat([pl.read_parquet(target), df]).unique(
            subset=["ts_code", "trade_date"], keep="last"
        )
    df.write_parquet(target)


def test_previous_close_is_converted_to_next_day_raw_basis():
    event = ReboundEvent(
        event_id="300308_20250102",
        symbol="300308",
        ts_code="300308.SZ",
        decision_date="20250102",
        close_d=20.0,
        position_60d_pct=10.0,
        drawdown_60d_pct=30.0,
        return_5d_pct=-8.0,
        avg_amount_20d=100_000_000.0,
        adj_factor_d=1.0,
    )

    assert _previous_close_on_next_day_basis(
        event,
        {"adj_factor": 2.0},
    ) == pytest.approx(10.0)
    assert _previous_close_on_next_day_basis(
        event,
        {"adj_factor": None},
    ) is None


def test_insufficient_evidence_consumes_the_formal_frozen_run(tmp_path):
    run_dir = tmp_path / "frozen-20260727-120000-000000"
    run_dir.mkdir()
    (run_dir / "summary.json").write_text(
        json.dumps({"verdict": "insufficient_evidence"}),
        "utf-8",
    )

    assert _find_completed_frozen_run(tmp_path) == run_dir.name


def _write_minute_partition(minute_root: Path, ts_code: str, trade_date: str, df: pl.DataFrame):
    """Write minute data to partitioned parquet (merges with existing)."""
    year = trade_date[:4]
    month = trade_date[4:6]
    partition_dir = minute_root / "raw" / "freq=5min" / f"ts_code={ts_code}" / f"year={year}" / f"month={month}"
    partition_dir.mkdir(parents=True, exist_ok=True)
    target = partition_dir / "part.parquet"
    # CRITICAL: Merge with existing data to avoid overwriting same-month days.
    if target.exists():
        existing = pl.read_parquet(target)
        combined = pl.concat([existing, df]).unique(
            subset=["ts_code", "trade_time", "freq"], keep="last"
        ).sort("trade_time")
        combined.write_parquet(target)
    else:
        df.write_parquet(target)


@pytest.fixture
def synthetic_env(tmp_path):
    """Create a synthetic data environment for E2E testing."""
    # Create directories
    data_root = tmp_path / "data"
    minute_root = tmp_path / "minute"
    runtime_root = tmp_path / "rebound-v1.1"
    data_root.mkdir(parents=True)
    minute_root.mkdir(parents=True)
    runtime_root.mkdir(parents=True)
    (data_root / "raw" / "suspend_d").mkdir(parents=True)

    # Create synthetic universe
    universe_path = tmp_path / "universe.json"
    universe = {
        "entries": [
            {"symbol": "000001", "pool_tier": "core"},
            {"symbol": "300308", "pool_tier": "core"},
        ]
    }
    universe_path.write_text(json.dumps(universe))

    # Generate synthetic data for development period (2025-01-01 to 2025-09-30)
    # We need enough trading days with events
    symbols = ["000001.SZ", "300308.SZ"]

    # Provide an authoritative pre-development history window. Minute bars are
    # unnecessary here because these sessions only build D-close features.
    history_dates = [
        (date(2024, 10, 1) + timedelta(days=offset)).strftime("%Y%m%d")
        for offset in range(75)
    ]
    for ts_code in symbols:
        base_price = 12.0 if ts_code == "000001.SZ" else 24.0
        for trade_date in history_dates:
            _write_daily_partition(
                data_root,
                trade_date,
                _make_synthetic_daily(ts_code, trade_date, base_price),
            )
    
    # Create daily data for development period
    dev_dates = []
    for month in range(1, 10):
        for day in range(1, 28):
            date_str = f"2025{month:02d}{day:02d}"
            dev_dates.append(date_str)
    
    for ts_code in symbols:
        base_price = 10.0 if ts_code == "000001.SZ" else 20.0
        for trade_date in dev_dates:
            # Create a declining pattern to trigger events
            day_num = int(trade_date[6:8])
            price = base_price * (1 - 0.005 * day_num)  # Gradual decline
            daily_df = _make_synthetic_daily(ts_code, trade_date, price)
            _write_daily_partition(data_root, trade_date, daily_df)
            
            minute_df = _make_synthetic_minute(ts_code, trade_date, price)
            _write_minute_partition(minute_root, ts_code, trade_date, minute_df)

    # Create daily data for validation period (2025-10-01 to 2026-02-23)
    val_dates = []
    for month in range(10, 13):
        for day in range(1, 28):
            date_str = f"2025{month:02d}{day:02d}"
            val_dates.append(date_str)
    for month in range(1, 3):
        for day in range(1, 24):
            date_str = f"2026{month:02d}{day:02d}"
            val_dates.append(date_str)

    for ts_code in symbols:
        base_price = 8.0 if ts_code == "000001.SZ" else 16.0
        for trade_date in val_dates:
            day_num = int(trade_date[6:8])
            price = base_price * (1 - 0.003 * day_num)
            daily_df = _make_synthetic_daily(ts_code, trade_date, price)
            _write_daily_partition(data_root, trade_date, daily_df)
            
            minute_df = _make_synthetic_minute(ts_code, trade_date, price)
            _write_minute_partition(minute_root, ts_code, trade_date, minute_df)

    # Create daily data for frozen period (2026-02-24 onwards)
    frozen_dates = []
    for day in range(24, 28):
        frozen_dates.append(f"202602{day:02d}")
    for month in range(3, 8):
        for day in range(1, 28):
            frozen_dates.append(f"2026{month:02d}{day:02d}")

    for ts_code in symbols:
        base_price = 7.0 if ts_code == "000001.SZ" else 14.0
        for trade_date in frozen_dates:
            day_num = int(trade_date[6:8])
            price = base_price * (1 - 0.002 * day_num)
            daily_df = _make_synthetic_daily(ts_code, trade_date, price)
            _write_daily_partition(data_root, trade_date, daily_df)
            
            minute_df = _make_synthetic_minute(ts_code, trade_date, price)
            _write_minute_partition(minute_root, ts_code, trade_date, minute_df)

    minute_dates = sorted(set(dev_dates + val_dates + frozen_dates))
    all_dates = sorted(set(history_dates + minute_dates))
    reference_root = data_root / "reference"
    reference_root.mkdir(parents=True)
    pl.DataFrame({
        "cal_date": all_dates,
        "is_open": [1] * len(all_dates),
    }).write_parquet(reference_root / "trade_cal.parquet")
    (data_root / "meta").mkdir(parents=True)
    (data_root / "meta" / "coverage.json").write_text(json.dumps({
        "source": "tushare-pro-point-in-time",
        "generated_at": datetime.now(UTC).isoformat(),
        "passed": True,
        "start_date": all_dates[0],
        "end_date": all_dates[-1],
        "trading_days": len(all_dates),
        "common_required_days": len(all_dates),
        "endpoint_days": {
            endpoint: len(all_dates)
            for endpoint in (
                "daily",
                "adj_factor",
                "daily_basic",
                "moneyflow",
                "stk_limit",
                "suspend_d",
            )
        },
        "missing_required_days": {},
        "reference_tables": {
            "stock_basic": True,
            "namechange": True,
            "trade_cal": True,
        },
        "failures": [],
    }))
    coverage_path = minute_root / "meta" / "coverage.json"
    coverage_path.parent.mkdir(parents=True)
    coverage_path.write_text(json.dumps({
        "quality_version": QUALITY_REPORT_VERSION,
        "source": "tushare_stk_mins",
        "generated_at": datetime.now(UTC).isoformat(),
        "passed": True,
        "coverage_pct": 100.0,
        "freq": "5min",
        "start_date": "2025-01-01",
        "end_date": "2026-07-27",
        "symbols_checked": len(symbols),
        "symbols_with_data": len(symbols),
        "total_bars": len(minute_dates) * len(symbols) * 48,
        "total_rows": len(minute_dates) * len(symbols) * 48,
        "zero_turnover_symbol_days": 0,
        "excluded_zero_turnover_bars": 0,
        "unexpected_zero_turnover_symbol_days": 0,
        "per_symbol_coverage": {symbol: 100.0 for symbol in symbols},
        "expected_symbols": symbols,
        "symbol_ranges": {
            symbol: {
                "first_time": "2025-01-01 09:35:00",
                "last_time": "2026-07-27 15:00:00",
            }
            for symbol in symbols
        },
        "duplicate_keys": 0,
        "non_monotonic_time": 0,
        "invalid_ohlc": 0,
        "invalid_numeric": 0,
        "missing_required_value": 0,
        "trade_date_mismatch": 0,
        "non_trading_session": 0,
        "missing_trading_days": 0,
        "missing_bars": 0,
        "issues": [],
        "failures": [],
    }))

    return {
        "data_root": data_root,
        "minute_root": minute_root,
        "runtime_root": runtime_root,
        "universe_path": universe_path,
        "tmp_path": tmp_path,
    }


class TestE2EPipeline:
    """End-to-end pipeline tests with synthetic data."""

    def test_development_stage(self, synthetic_env):
        """Development stage runs successfully."""
        config_path = Path(__file__).parent.parent / "config" / "rebound-v1.1.json"
        summary = run_rebound_study(
            config_path=config_path,
            stage="development",
            runtime_root=synthetic_env["runtime_root"],
            minute_root=synthetic_env["minute_root"],
            daily_data_root=synthetic_env["data_root"],
            universe_path=synthetic_env["universe_path"],
            repo_root=synthetic_env["tmp_path"],
        )
        assert summary.stage == "development"
        # Development should complete without blocking
        assert summary.verdict not in ("blocked", "blocked_by_daily_data")
        # Must have strategy results (3 strategies x 3 hold periods = 9)
        assert len(summary.strategies) == 9
        # latest.json must be written
        latest_path = synthetic_env["runtime_root"] / "latest.json"
        assert latest_path.exists()
        latest = json.loads(latest_path.read_text())
        assert "development" in latest
        # Run dir must exist with summary.json
        dev_run_dir = synthetic_env["runtime_root"] / latest["development"]
        assert (dev_run_dir / "summary.json").exists()
        assert (dev_run_dir / "manifest.json").exists()
        quality = json.loads((dev_run_dir / "quality.json").read_text())
        manifest = json.loads((dev_run_dir / "manifest.json").read_text())
        assert quality["passed"] is True
        assert quality["coverage_sha256"]
        assert quality["daily_coverage"]["passed"] is True
        assert quality["daily_coverage"]["coverage_sha256"]
        assert manifest["data_coverage"]["coverage_sha256"]
        assert manifest["data_coverage"]["daily_coverage"][
            "coverage_sha256"
        ]
        assert manifest["data_coverage"]["start_date"] == "2025-01-01"

    def test_validation_stage(self, synthetic_env):
        """Validation stage runs successfully."""
        config_path = Path(__file__).parent.parent / "config" / "rebound-v1.1.json"
        run_rebound_study(
            config_path=config_path,
            stage="development",
            runtime_root=synthetic_env["runtime_root"],
            minute_root=synthetic_env["minute_root"],
            daily_data_root=synthetic_env["data_root"],
            universe_path=synthetic_env["universe_path"],
            repo_root=synthetic_env["tmp_path"],
        )
        summary = run_rebound_study(
            config_path=config_path,
            stage="validation",
            runtime_root=synthetic_env["runtime_root"],
            minute_root=synthetic_env["minute_root"],
            daily_data_root=synthetic_env["data_root"],
            universe_path=synthetic_env["universe_path"],
            repo_root=synthetic_env["tmp_path"],
        )
        assert summary.stage == "validation"
        assert summary.verdict not in ("blocked", "blocked_by_daily_data")

    def test_validation_blocks_without_development_evidence(
        self, synthetic_env
    ):
        config_path = (
            Path(__file__).parent.parent
            / "config"
            / "rebound-v1.1.json"
        )

        summary = run_rebound_study(
            config_path=config_path,
            stage="validation",
            runtime_root=synthetic_env["runtime_root"],
            minute_root=synthetic_env["minute_root"],
            daily_data_root=synthetic_env["data_root"],
            universe_path=synthetic_env["universe_path"],
            repo_root=synthetic_env["tmp_path"],
        )

        assert summary.verdict == "blocked"
        assert "development evidence" in summary.note

    def test_lock_and_frozen(self, synthetic_env):
        """Lock config and run frozen stage."""
        base_config_path = (
            Path(__file__).parent.parent / "config" / "rebound-v1.1.json"
        )
        frozen_config = json.loads(base_config_path.read_text("utf-8"))
        frozen_config["event_thresholds"].update(
            {
                "max_60d_position_pct": 100.0,
                "min_60d_drawdown_pct": 0.0,
                "max_5d_return_pct": 100.0,
                "min_20d_avg_amount": 1.0,
            }
        )
        frozen_config["selection_rules"].update(
            {
                "min_frozen_trading_days": 1,
                "min_frozen_events": 1,
            }
        )
        config_path = synthetic_env["tmp_path"] / "frozen-config.json"
        config_path.write_text(json.dumps(frozen_config), "utf-8")
        
        # Coverage is created by the synthetic fixture and already admitted.
        coverage_path = synthetic_env["minute_root"] / "meta" / "coverage.json"
        config = json.loads(config_path.read_text("utf-8"))
        config_hash = hash_config(config)
        coverage_hash = hashlib.sha256(coverage_path.read_bytes()).hexdigest()
        daily_coverage_path = (
            synthetic_env["data_root"] / "meta" / "coverage.json"
        )
        daily_coverage_hash = hashlib.sha256(
            daily_coverage_path.read_bytes()
        ).hexdigest()
        strategies = []
        for entry in config["entry_strategies"]:
            for hold_days in config["hold_periods"]:
                selected = entry["name"] == "next_open" and hold_days == 1
                strategies.append({
                    "strategy_name": entry["name"],
                    "hold_days": hold_days,
                    "filled_trades": 30,
                    "mean_net_return": 0.01 if selected else -0.01,
                    "expected_profit_per_100k": 50.0 if selected else -10.0,
                    "return_cvar_ratio": 1.0 if selected else -1.0,
                    "max_drawdown": -0.02 if selected else -0.05,
                })
        dev_report_path = synthetic_env["runtime_root"] / "lock-dev.json"
        val_report_path = synthetic_env["runtime_root"] / "lock-val.json"
        dev_report_path.write_text(json.dumps({
            "stage": "development",
            "config_hash": config_hash,
            "coverage_sha256": coverage_hash,
            "daily_coverage_sha256": daily_coverage_hash,
            "universe_sha256": hashlib.sha256(
                synthetic_env["universe_path"].read_bytes()
            ).hexdigest(),
            "verdict": "development_complete",
            "strategies": strategies,
        }))
        val_report_path.write_text(json.dumps({
            "stage": "validation",
            "config_hash": config_hash,
            "coverage_sha256": coverage_hash,
            "daily_coverage_sha256": daily_coverage_hash,
            "universe_sha256": hashlib.sha256(
                synthetic_env["universe_path"].read_bytes()
            ).hexdigest(),
            "verdict": "viable",
            "selected_strategy": "next_open_1d",
            "strategies": strategies,
        }))
        original_val_report = val_report_path.read_text("utf-8")
        incomplete_val = json.loads(original_val_report)
        incomplete_val["strategies"] = incomplete_val["strategies"][:-1]
        val_report_path.write_text(json.dumps(incomplete_val))
        with pytest.raises(ValueError, match="strategy set is incomplete"):
            create_config_lock(
                config_path=config_path,
                coverage_report_path=coverage_path,
                selected_strategy="next_open_1d",
                dev_report_path=str(dev_report_path),
                val_report_path=str(val_report_path),
                output_path=synthetic_env["runtime_root"] / "bad-lock.json",
                universe_path=synthetic_env["universe_path"],
            )
        val_report_path.write_text(original_val_report)

        non_finite_val = json.loads(original_val_report)
        non_finite_val["strategies"][0][
            "expected_profit_per_100k"
        ] = float("inf")
        val_report_path.write_text(json.dumps(non_finite_val))
        with pytest.raises(ValueError, match="not valid JSON"):
            create_config_lock(
                config_path=config_path,
                coverage_report_path=coverage_path,
                selected_strategy="next_open_1d",
                dev_report_path=str(dev_report_path),
                val_report_path=str(val_report_path),
                output_path=synthetic_env["runtime_root"] / "bad-lock.json",
                universe_path=synthetic_env["universe_path"],
            )
        val_report_path.write_text(original_val_report)

        short_coverage_path = (
            synthetic_env["minute_root"] / "meta" / "short-coverage.json"
        )
        short_coverage = json.loads(coverage_path.read_text("utf-8"))
        short_coverage["end_date"] = "2025-12-31"
        short_coverage_path.write_text(json.dumps(short_coverage))
        with pytest.raises(ValueError, match="does not span"):
            create_config_lock(
                config_path=config_path,
                coverage_report_path=short_coverage_path,
                selected_strategy="next_open_1d",
                dev_report_path=str(dev_report_path),
                val_report_path=str(val_report_path),
                output_path=synthetic_env["runtime_root"] / "short-lock.json",
                universe_path=synthetic_env["universe_path"],
            )

        # Create config lock with coverage report
        lock = create_config_lock(
            config_path=config_path,
            coverage_report_path=coverage_path,
            selected_strategy="next_open_1d",
            dev_report_path=str(dev_report_path),
            val_report_path=str(val_report_path),
            output_path=synthetic_env["runtime_root"] / "config-lock.json",
            universe_path=synthetic_env["universe_path"],
        )
        assert "config_sha256" in lock
        assert "locked_at" in lock
        assert lock["coverage_sha256"] != ""  # Coverage hash must be non-empty
        assert lock["coverage_report_path"] == str(coverage_path)
        assert lock["daily_coverage_sha256"] == daily_coverage_hash
        assert lock["daily_coverage_report_path"] == str(
            daily_coverage_path
        )
        assert lock["universe_path"] == str(
            synthetic_env["universe_path"]
        )
        assert lock["lock_version"] == 3

        # Verify lock
        verified, msg = verify_config_lock(config_path, synthetic_env["runtime_root"] / "config-lock.json")
        assert verified, f"Config lock verification failed: {msg}"

        original_coverage = coverage_path.read_text("utf-8")
        mutated_coverage = json.loads(original_coverage)
        mutated_coverage["coverage_pct"] = 99.9
        coverage_path.write_text(json.dumps(mutated_coverage))
        verified, msg = verify_config_lock(
            config_path,
            synthetic_env["runtime_root"] / "config-lock.json",
        )
        assert not verified
        assert "hash mismatch" in msg
        coverage_path.write_text(original_coverage)

        original_daily_coverage = daily_coverage_path.read_text("utf-8")
        mutated_daily_coverage = json.loads(original_daily_coverage)
        mutated_daily_coverage["generated_at"] = (
            "2026-07-28T00:00:00+00:00"
        )
        daily_coverage_path.write_text(json.dumps(mutated_daily_coverage))
        verified, msg = verify_config_lock(
            config_path,
            synthetic_env["runtime_root"] / "config-lock.json",
        )
        assert not verified
        assert "hash mismatch" in msg
        daily_coverage_path.write_text(original_daily_coverage)

        original_universe = synthetic_env["universe_path"].read_text("utf-8")
        mutated_universe = json.loads(original_universe)
        mutated_universe["note"] = "changed after lock"
        synthetic_env["universe_path"].write_text(
            json.dumps(mutated_universe),
            "utf-8",
        )
        verified, msg = verify_config_lock(
            config_path,
            synthetic_env["runtime_root"] / "config-lock.json",
        )
        assert not verified
        assert "universe hash mismatch" in msg
        synthetic_env["universe_path"].write_text(
            original_universe,
            "utf-8",
        )

        alternate_minute_root = (
            synthetic_env["tmp_path"] / "alternate-minute"
        )
        alternate_coverage = (
            alternate_minute_root / "meta" / "coverage.json"
        )
        alternate_coverage.parent.mkdir(parents=True)
        alternate_coverage.write_text(
            coverage_path.read_text("utf-8"),
            "utf-8",
        )
        wrong_source = run_rebound_study(
            config_path=config_path,
            stage="frozen",
            runtime_root=synthetic_env["runtime_root"],
            minute_root=alternate_minute_root,
            daily_data_root=synthetic_env["data_root"],
            universe_path=synthetic_env["universe_path"],
            repo_root=synthetic_env["tmp_path"],
        )
        assert wrong_source.verdict == "blocked"
        assert "sources do not match" in wrong_source.note

        # Run frozen stage
        frozen_summary = run_rebound_study(
            config_path=config_path,
            stage="frozen",
            runtime_root=synthetic_env["runtime_root"],
            minute_root=synthetic_env["minute_root"],
            daily_data_root=synthetic_env["data_root"],
            universe_path=synthetic_env["universe_path"],
            repo_root=synthetic_env["tmp_path"],
        )
        assert frozen_summary.stage == "frozen"
        assert frozen_summary.verdict == "frozen_complete"
        assert len(frozen_summary.strategies) == 1
        assert frozen_summary.selected_strategy == "next_open_1d"

        original_val = val_report_path.read_text("utf-8")
        val_report_path.unlink()
        verified, msg = verify_config_lock(
            config_path,
            synthetic_env["runtime_root"] / "config-lock.json",
        )
        assert not verified
        assert "not found" in msg
        val_report_path.write_text(original_val)

        refused = run_rebound_study(
            config_path=config_path,
            stage="frozen",
            runtime_root=synthetic_env["runtime_root"],
            minute_root=synthetic_env["minute_root"],
            daily_data_root=synthetic_env["data_root"],
            universe_path=synthetic_env["universe_path"],
            repo_root=synthetic_env["tmp_path"],
        )
        assert refused.verdict == "blocked"
        assert "already completed" in refused.note

    def test_reproducibility(self, synthetic_env):
        """Same input produces same output."""
        config_path = Path(__file__).parent.parent / "config" / "rebound-v1.1.json"
        
        summary1 = run_rebound_study(
            config_path=config_path,
            stage="development",
            runtime_root=synthetic_env["runtime_root"],
            minute_root=synthetic_env["minute_root"],
            daily_data_root=synthetic_env["data_root"],
            universe_path=synthetic_env["universe_path"],
            repo_root=synthetic_env["tmp_path"],
        )
        summary2 = run_rebound_study(
            config_path=config_path,
            stage="development",
            runtime_root=synthetic_env["runtime_root"],
            minute_root=synthetic_env["minute_root"],
            daily_data_root=synthetic_env["data_root"],
            universe_path=synthetic_env["universe_path"],
            repo_root=synthetic_env["tmp_path"],
        )
        
        # Compare key metrics
        assert summary1.config_hash == summary2.config_hash
        assert len(summary1.strategies) == len(summary2.strategies)
        # Compare strategy results
        for s1, s2 in zip(summary1.strategies, summary2.strategies, strict=True):
            assert s1.to_dict() == s2.to_dict()

    def test_frozen_blocked_without_lock(self, synthetic_env):
        """Frozen stage is blocked without config lock."""
        config_path = Path(__file__).parent.parent / "config" / "rebound-v1.1.json"
        
        summary = run_rebound_study(
            config_path=config_path,
            stage="frozen",
            runtime_root=synthetic_env["runtime_root"],
            minute_root=synthetic_env["minute_root"],
            daily_data_root=synthetic_env["data_root"],
            universe_path=synthetic_env["universe_path"],
            repo_root=synthetic_env["tmp_path"],
        )
        assert summary.verdict == "blocked"
        assert "config lock" in summary.note.lower()

    def test_missing_minute_coverage_blocks_before_ranking(
        self, synthetic_env
    ):
        config_path = (
            Path(__file__).parent.parent
            / "config"
            / "rebound-v1.1.json"
        )
        (
            synthetic_env["minute_root"] / "meta" / "coverage.json"
        ).unlink()

        summary = run_rebound_study(
            config_path=config_path,
            stage="development",
            runtime_root=synthetic_env["runtime_root"],
            minute_root=synthetic_env["minute_root"],
            daily_data_root=synthetic_env["data_root"],
            universe_path=synthetic_env["universe_path"],
            repo_root=synthetic_env["tmp_path"],
        )

        assert summary.verdict == "blocked"
        assert summary.strategies == []
        latest = json.loads(
            (synthetic_env["runtime_root"] / "latest.json").read_text()
        )
        run_dir = synthetic_env["runtime_root"] / latest["development"]
        quality = json.loads((run_dir / "quality.json").read_text())
        assert quality["passed"] is False
        assert (run_dir / "events.parquet").exists()
        assert (run_dir / "trades.parquet").exists()

    def test_legacy_minute_quality_report_blocks_before_ranking(
        self, synthetic_env
    ):
        config_path = (
            Path(__file__).parent.parent
            / "config"
            / "rebound-v1.1.json"
        )
        coverage_path = (
            synthetic_env["minute_root"] / "meta" / "coverage.json"
        )
        coverage = json.loads(coverage_path.read_text("utf-8"))
        coverage.pop("quality_version")
        coverage_path.write_text(json.dumps(coverage), "utf-8")

        summary = run_rebound_study(
            config_path=config_path,
            stage="development",
            runtime_root=synthetic_env["runtime_root"],
            minute_root=synthetic_env["minute_root"],
            daily_data_root=synthetic_env["data_root"],
            universe_path=synthetic_env["universe_path"],
            repo_root=synthetic_env["tmp_path"],
        )

        assert summary.verdict == "blocked"
        assert "quality_version" in summary.note

    def test_inconsistent_zero_turnover_accounting_blocks_before_ranking(
        self, synthetic_env
    ):
        config_path = (
            Path(__file__).parent.parent
            / "config"
            / "rebound-v1.1.json"
        )
        coverage_path = (
            synthetic_env["minute_root"] / "meta" / "coverage.json"
        )
        coverage = json.loads(coverage_path.read_text("utf-8"))
        coverage["excluded_zero_turnover_bars"] = 48
        coverage_path.write_text(json.dumps(coverage), "utf-8")

        summary = run_rebound_study(
            config_path=config_path,
            stage="development",
            runtime_root=synthetic_env["runtime_root"],
            minute_root=synthetic_env["minute_root"],
            daily_data_root=synthetic_env["data_root"],
            universe_path=synthetic_env["universe_path"],
            repo_root=synthetic_env["tmp_path"],
        )

        assert summary.verdict == "blocked"
        assert "zero-turnover accounting" in summary.note

    def test_failed_daily_coverage_blocks_before_ranking(
        self, synthetic_env
    ):
        config_path = (
            Path(__file__).parent.parent
            / "config"
            / "rebound-v1.1.json"
        )
        daily_coverage_path = (
            synthetic_env["data_root"] / "meta" / "coverage.json"
        )
        daily_coverage = json.loads(
            daily_coverage_path.read_text("utf-8")
        )
        daily_coverage["passed"] = False
        daily_coverage["failures"] = ["adj_factor missing one day"]
        daily_coverage_path.write_text(json.dumps(daily_coverage), "utf-8")

        summary = run_rebound_study(
            config_path=config_path,
            stage="development",
            runtime_root=synthetic_env["runtime_root"],
            minute_root=synthetic_env["minute_root"],
            daily_data_root=synthetic_env["data_root"],
            universe_path=synthetic_env["universe_path"],
            repo_root=synthetic_env["tmp_path"],
        )

        assert summary.verdict == "blocked_by_daily_data"
        assert summary.strategies == []
        latest = json.loads(
            (synthetic_env["runtime_root"] / "latest.json").read_text()
        )
        run_dir = synthetic_env["runtime_root"] / latest["development"]
        quality = json.loads((run_dir / "quality.json").read_text())
        assert quality["passed"] is False
        assert quality["daily_coverage"]["passed"] is False
        assert any(
            "adj_factor missing one day" in failure
            for failure in quality["daily_coverage"]["failures"]
        )

    def test_malformed_minute_coverage_blocks(self, synthetic_env):
        config_path = (
            Path(__file__).parent.parent
            / "config"
            / "rebound-v1.1.json"
        )
        coverage_path = (
            synthetic_env["minute_root"] / "meta" / "coverage.json"
        )
        coverage_path.write_text("{not-json", "utf-8")

        summary = run_rebound_study(
            config_path=config_path,
            stage="development",
            runtime_root=synthetic_env["runtime_root"],
            minute_root=synthetic_env["minute_root"],
            daily_data_root=synthetic_env["data_root"],
            universe_path=synthetic_env["universe_path"],
            repo_root=synthetic_env["tmp_path"],
        )

        assert summary.verdict == "blocked"
        assert "invalid JSON" in summary.note

    def test_coverage_subset_cannot_claim_full_universe(
        self, synthetic_env
    ):
        config_path = (
            Path(__file__).parent.parent
            / "config"
            / "rebound-v1.1.json"
        )
        coverage_path = (
            synthetic_env["minute_root"] / "meta" / "coverage.json"
        )
        coverage = json.loads(coverage_path.read_text("utf-8"))
        retained = "000001.SZ"
        coverage["expected_symbols"] = [retained]
        coverage["per_symbol_coverage"] = {retained: 100.0}
        coverage["symbol_ranges"] = {
            retained: coverage["symbol_ranges"][retained]
        }
        coverage["symbols_checked"] = 1
        coverage["symbols_with_data"] = 1
        coverage_path.write_text(json.dumps(coverage), "utf-8")

        summary = run_rebound_study(
            config_path=config_path,
            stage="development",
            runtime_root=synthetic_env["runtime_root"],
            minute_root=synthetic_env["minute_root"],
            daily_data_root=synthetic_env["data_root"],
            universe_path=synthetic_env["universe_path"],
            repo_root=synthetic_env["tmp_path"],
        )

        assert summary.verdict == "blocked"
        assert "research universe" in summary.note
        latest = json.loads(
            (synthetic_env["runtime_root"] / "latest.json").read_text()
        )
        run_dir = synthetic_env["runtime_root"] / latest["development"]
        quality = json.loads((run_dir / "quality.json").read_text())
        assert quality["passed"] is False
        assert any("research universe" in item for item in quality["failures"])

    def test_future_universe_member_does_not_invalidate_old_coverage(
        self, synthetic_env
    ):
        config_path = (
            Path(__file__).parent.parent
            / "config"
            / "rebound-v1.1.json"
        )
        universe_path = synthetic_env["universe_path"]
        universe = json.loads(universe_path.read_text("utf-8"))
        universe["entries"].append(
            {
                "symbol": "688825",
                "pool_tier": "core",
                "strategy_from": "2026-07-28",
            }
        )
        universe_path.write_text(json.dumps(universe), "utf-8")

        summary = run_rebound_study(
            config_path=config_path,
            stage="development",
            runtime_root=synthetic_env["runtime_root"],
            minute_root=synthetic_env["minute_root"],
            daily_data_root=synthetic_env["data_root"],
            universe_path=universe_path,
            repo_root=synthetic_env["tmp_path"],
        )

        assert summary.verdict != "blocked"
        assert "research universe" not in summary.note

    def test_coverage_with_extra_symbol_cannot_enter_research(
        self, synthetic_env
    ):
        config_path = (
            Path(__file__).parent.parent
            / "config"
            / "rebound-v1.1.json"
        )
        coverage_path = (
            synthetic_env["minute_root"] / "meta" / "coverage.json"
        )
        coverage = json.loads(coverage_path.read_text("utf-8"))
        extra = "688256.SH"
        coverage["per_symbol_coverage"][extra] = 100.0
        coverage["symbol_ranges"][extra] = {
            "first_time": "2025-01-01 09:35:00",
            "last_time": "2026-07-27 15:00:00",
        }
        coverage["symbols_checked"] += 1
        coverage["symbols_with_data"] += 1
        coverage_path.write_text(json.dumps(coverage), "utf-8")

        summary = run_rebound_study(
            config_path=config_path,
            stage="development",
            runtime_root=synthetic_env["runtime_root"],
            minute_root=synthetic_env["minute_root"],
            daily_data_root=synthetic_env["data_root"],
            universe_path=synthetic_env["universe_path"],
            repo_root=synthetic_env["tmp_path"],
        )

        assert summary.verdict == "blocked"
        assert "outside the research universe" in summary.note

    def test_daily_coverage_must_include_feature_lookback(
        self, synthetic_env
    ):
        config_path = (
            Path(__file__).parent.parent
            / "config"
            / "rebound-v1.1.json"
        )
        daily_coverage_path = (
            synthetic_env["data_root"] / "meta" / "coverage.json"
        )
        coverage = json.loads(daily_coverage_path.read_text("utf-8"))
        coverage["start_date"] = "20250101"
        daily_coverage_path.write_text(json.dumps(coverage), "utf-8")

        summary = run_rebound_study(
            config_path=config_path,
            stage="development",
            runtime_root=synthetic_env["runtime_root"],
            minute_root=synthetic_env["minute_root"],
            daily_data_root=synthetic_env["data_root"],
            universe_path=synthetic_env["universe_path"],
            repo_root=synthetic_env["tmp_path"],
        )

        assert summary.verdict == "blocked_by_daily_data"
        assert "does not span" in summary.note
