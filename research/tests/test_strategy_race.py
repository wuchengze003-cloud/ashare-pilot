import json
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

import polars as pl
import pytest

from ashare_research.data_sync import DatasetCoverage
from ashare_research.features import FEATURE_VERSION
from ashare_research.portfolio import EquityPoint, PortfolioResult
from ashare_research.race_config import RaceWindow, load_race_config
from ashare_research.strategy_race import (
    CandidateRaceResult,
    WindowMetrics,
    _load_data_quality_evidence,
    _market_capture,
    _purged_window,
    _rolling_folds,
    _training_window_before,
    apply_calibrator,
    attach_industry_metadata,
    filter_to_instrument_membership,
    fit_decile_calibrator,
    load_instrument_intervals,
    select_daily_candidate,
)

CONFIG = Path(__file__).parent.parent / "config" / "production-race-v1.json"


def test_membership_filter_uses_date_bounded_intervals(tmp_path):
    membership = tmp_path / "members.txt"
    membership.write_text(
        "SZ000001\t2025-01-02\t2025-01-03\n"
        "SZ000001\t2025-01-06\t2025-01-07\n",
        "utf-8",
    )
    frame = pl.DataFrame(
        {
            "date": [
                date(2025, 1, 1),
                date(2025, 1, 2),
                date(2025, 1, 3),
                date(2025, 1, 4),
                date(2025, 1, 6),
            ],
            "symbol": ["sz000001"] * 5,
            "value": range(5),
        }
    )

    filtered = filter_to_instrument_membership(
        frame,
        load_instrument_intervals(membership),
    )

    assert filtered["date"].to_list() == [
        date(2025, 1, 2),
        date(2025, 1, 3),
        date(2025, 1, 4),
        date(2025, 1, 6),
    ]
    assert filtered["is_universe_member"].to_list() == [
        True,
        True,
        False,
        True,
    ]


def test_industry_risk_metadata_is_point_in_time(tmp_path):
    membership = tmp_path / "sw.parquet"
    pl.DataFrame(
        {
            "ts_code": ["000001.SZ", "000001.SZ"],
            "l1_code": ["801780.SI", "801790.SI"],
            "l1_name": ["银行", "非银金融"],
            "in_date": ["20200101", "20250103"],
            "out_date": ["20250102", None],
        }
    ).write_parquet(membership)
    frame = pl.DataFrame(
        {
            "date": [
                date(2025, 1, 2),
                date(2025, 1, 3),
                date(2025, 1, 4),
            ],
            "symbol": ["sz000001"] * 3,
        }
    )

    attached = attach_industry_metadata(frame, membership)

    assert attached["theme"].to_list() == [
        "801780.SI:银行",
        "801790.SI:非银金融",
        "801790.SI:非银金融",
    ]


def test_calibration_never_uses_labels_after_training_window():
    start = date(2025, 1, 1)
    frame = pl.DataFrame(
        {
            "date": [start + timedelta(days=index) for index in range(12)],
            "symbol": [f"sz{index:06d}" for index in range(12)],
            "eligible": [True] * 12,
            "raw_score": [float(index) for index in range(12)],
            "label_return_5": [index / 100 for index in range(12)],
        }
    )
    training = RaceWindow(start, start + timedelta(days=7))
    baseline = fit_decile_calibrator(frame, training, 2, 2)
    changed = fit_decile_calibrator(
        frame.with_columns(
            pl.when(pl.col("date") > training.end)
            .then(pl.lit(-100.0))
            .otherwise(pl.col("label_return_5"))
            .alias("label_return_5")
        ),
        training,
        2,
        2,
    )

    assert changed == baseline
    assert apply_calibrator(frame, baseline)["prediction"].equals(
        apply_calibrator(frame, changed)["prediction"]
    )


def test_market_capture_aligns_actual_trade_dates_not_calendar_days():
    result = PortfolioResult(
        total_return_pct=1,
        sharpe=1,
        max_drawdown_pct=0,
        cvar_5_pct=0,
        turnover_pct=0,
        average_hold_bars=1,
        closed_trades=1,
        equity_curve=(
            EquityPoint(date(2026, 7, 27), 101.0, 0.0, 1),
        ),
        trades=(),
    )
    scored = pl.DataFrame(
        {
            "date": [date(2026, 7, 24), date(2026, 7, 27)],
            "market_return": [0.10, 0.02],
        }
    )

    upside, downside = _market_capture(result, scored, 100.0)

    assert upside == pytest.approx(0.5)
    assert downside is None


def test_walk_forward_windows_are_ordered_and_purged():
    start = date(2025, 1, 1)
    dates = [start + timedelta(days=index) for index in range(180)]
    folds = _rolling_folds(dates, RaceWindow(start, dates[-1]), fold_days=63)

    assert len(folds) == 3
    assert all(left.end < right.start for left, right in zip(folds, folds[1:], strict=False))
    training = _training_window_before(dates, folds[1].start)
    pre_fold_dates = [value for value in dates if value < folds[1].start]
    assert training.end == pre_fold_dates[-6]


def test_quality_evidence_is_bound_to_panel_and_coverage(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    panel_path = runtime / "features" / "panel.parquet"
    quality_path = runtime / "quality" / "feature-panel.json"
    panel_path.parent.mkdir(parents=True)
    quality_path.parent.mkdir(parents=True)
    panel = pl.DataFrame(
        {
            "date": [date(2025, 1, 2), date(2025, 1, 3)],
            "symbol": ["sz000001", "sz000001"],
        }
    )
    panel.write_parquet(panel_path)
    panel_path.with_suffix(".manifest.json").write_text(
        json.dumps(
            {
                "feature_version": FEATURE_VERSION,
                "rows": 2,
                "start_date": "2025-01-02",
                "end_date": "2025-01-03",
                "as_of_date": "2025-01-03",
            }
        ),
        "utf-8",
    )
    quality_path.write_text(
        json.dumps(
            {
                "passed": True,
                "rows": 2,
                "duplicate_keys": 0,
                "future_rows": 0,
                "failures": [],
            }
        ),
        "utf-8",
    )
    coverage = DatasetCoverage(
        source="test",
        generated_at="2026-07-28T00:00:00+00:00",
        passed=True,
        start_date="20250102",
        end_date="20250103",
        trading_days=2,
        common_required_days=2,
        endpoint_days={},
        missing_required_days={},
        reference_tables={},
        failures=(),
    )
    monkeypatch.setattr(
        "ashare_research.strategy_race.assert_production_dataset",
        lambda *_args, **_kwargs: coverage,
    )
    config = replace(
        load_race_config(CONFIG),
        minimum_complete_daily_trading_days=2,
    )

    complete_days, evidence_hash = _load_data_quality_evidence(
        panel_path,
        panel,
        config,
        quality_path,
    )

    assert complete_days == 2
    assert len(evidence_hash) == 64


def _metrics(sharpe: float, total_return: float = 20.0) -> WindowMetrics:
    return WindowMetrics(
        trading_days=252,
        total_return_pct=total_return,
        annualized_return_pct=total_return,
        sharpe=sharpe,
        max_drawdown_pct=-10,
        calmar=2,
        cvar_5_pct=-1,
        turnover_pct=100,
        average_hold_bars=5,
        closed_trades=120,
    )


def _candidate(candidate_id: str, score: float, passed: bool) -> CandidateRaceResult:
    return CandidateRaceResult(
        candidate_id=candidate_id,
        family=candidate_id,
        signal_frequency="1d",
        calibration_label="label_excess_return_10",
        selected_params={},
        validation_objective=score,
        validation_fold_sharpes=(score,) * 4,
        median_validation_fold_sharpe=score,
        positive_validation_fold_share=1,
        validation=_metrics(score),
        oos=_metrics(score),
        frozen=_metrics(score),
        double_cost_oos=_metrics(score),
        oos_fold_sharpes=(score,) * 4,
        median_oos_fold_sharpe=score,
        positive_oos_fold_share=1,
        oos_upside_capture=0.8,
        oos_downside_capture=0.5,
        bootstrap_probability_sharpe_positive=0.99,
        daily_gate_results={"test": passed},
        daily_gates_passed=passed,
    )


def test_selection_never_promotes_a_candidate_that_failed_a_gate():
    result = select_daily_candidate(
        (
            _candidate("failed-high-score", 3.0, False),
            _candidate("passing", 1.8, True),
            _candidate("failed", 1.0, False),
        )
    )

    assert result == "passing"


def test_calibration_window_purges_the_full_label_horizon():
    start = date(2024, 1, 1)
    dates = [start + timedelta(days=index) for index in range(40)]
    window = RaceWindow(dates[0], dates[-1])

    purged = _purged_window(dates, window, 10)

    assert purged.start == dates[0]
    assert purged.end == dates[-11]
