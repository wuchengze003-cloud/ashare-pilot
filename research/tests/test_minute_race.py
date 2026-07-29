from datetime import date, datetime, timedelta

import polars as pl

from ashare_research.minute_portfolio import MinuteRequirement
from ashare_research.minute_race import (
    MinuteCandidateRaceResult,
    _audit_required_data,
    _write_evidence,
    select_production_champion,
)
from ashare_research.portfolio import PortfolioResult
from ashare_research.strategy_race import WindowMetrics


def _metrics(sharpe: float) -> WindowMetrics:
    return WindowMetrics(
        trading_days=252,
        total_return_pct=25,
        annualized_return_pct=20,
        sharpe=sharpe,
        max_drawdown_pct=-10,
        calmar=2,
        cvar_5_pct=-1,
        turnover_pct=500,
        average_hold_bars=6,
        closed_trades=120,
    )


def _candidate(
    candidate_id: str,
    sharpe: float,
    passed: bool,
) -> MinuteCandidateRaceResult:
    return MinuteCandidateRaceResult(
        candidate_id=candidate_id,
        family=candidate_id,
        daily_params={},
        minute_params={},
        validation_objective=sharpe,
        validation=_metrics(sharpe),
        oos=_metrics(sharpe),
        frozen=_metrics(sharpe),
        double_cost_oos=_metrics(sharpe),
        oos_fold_sharpes=(sharpe,) * 4,
        median_oos_fold_sharpe=sharpe,
        positive_oos_fold_share=1,
        oos_upside_capture=0.8,
        oos_downside_capture=0.5,
        bootstrap_probability_sharpe_positive=0.99,
        daily_oos_sharpe=sharpe - 0.1,
        integrated_gate_results={"test": passed},
        integrated_gates_passed=passed,
    )


def test_final_race_never_promotes_a_failed_candidate():
    champion = select_production_champion(
        (
            _candidate("failed-high", 3.0, False),
            _candidate("passing", 1.8, True),
            _candidate("failed-low", 1.0, False),
        )
    )

    assert champion == "passing"


def test_final_race_keeps_production_empty_when_all_candidates_fail():
    champion = select_production_champion(
        (
            _candidate("one", 3.0, False),
            _candidate("two", 2.0, False),
            _candidate("three", 1.0, False),
        )
    )

    assert champion is None


def test_empty_trade_evidence_is_still_a_valid_parquet_artifact(tmp_path):
    result = PortfolioResult(0, 0, 0, 0, 0, 0, 0, (), ())

    _write_evidence(tmp_path, "anchor-v1", "oos", result)

    assert (tmp_path / "anchor-v1-oos-equity.parquet").is_file()
    assert (tmp_path / "anchor-v1-oos-trades.parquet").is_file()


def _minute_bars(value: date) -> pl.DataFrame:
    start = datetime.combine(value, datetime.min.time()) + timedelta(
        hours=9,
        minutes=35,
    )
    times = [start + timedelta(minutes=5 * index) for index in range(24)]
    afternoon = datetime.combine(
        value,
        datetime.min.time(),
    ) + timedelta(hours=13, minutes=5)
    times.extend(
        afternoon + timedelta(minutes=5 * index) for index in range(24)
    )
    return pl.DataFrame(
        {
            "trade_date": [value.strftime("%Y%m%d")] * 48,
            "trade_time": [
                item.strftime("%Y-%m-%d %H:%M:%S") for item in times
            ],
            "open": [10.0] * 48,
            "high": [10.1] * 48,
            "low": [9.9] * 48,
            "close": [10.0] * 48,
            "volume": [1000.0] * 48,
            "amount": [10_000.0] * 48,
        }
    )


def test_minute_coverage_audit_reports_partial_data_without_hash():
    first = MinuteRequirement("sz000001", date(2026, 7, 20))
    second = MinuteRequirement("sh600000", date(2026, 7, 20))

    audit = _audit_required_data(
        (first, second),
        lambda symbol, value: (
            _minute_bars(value)
            if symbol == first.symbol
            else pl.DataFrame()
        ),
    )

    assert audit.passed is False
    assert audit.required_symbol_days == 2
    assert audit.available_symbol_days == 1
    assert audit.available_symbols == 1
    assert audit.coverage_pct == 50
    assert audit.missing_preview == ("sh600000@2026-07-20",)
    assert audit.data_sha256 is None


def test_minute_coverage_audit_hashes_only_complete_requirements():
    requirement = MinuteRequirement("sz000001", date(2026, 7, 20))

    audit = _audit_required_data(
        (requirement,),
        lambda _symbol, value: _minute_bars(value),
    )

    assert audit.passed is True
    assert audit.coverage_pct == 100
    assert audit.data_sha256 is not None
