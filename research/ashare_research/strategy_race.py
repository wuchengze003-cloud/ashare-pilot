"""Pre-registered walk-forward race for the three production candidates."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass, replace
from datetime import UTC, date, datetime
from pathlib import Path
from statistics import median
from typing import Any

import numpy as np
import polars as pl
from sklearn.isotonic import IsotonicRegression

from .data_sync import assert_production_dataset
from .features import FEATURE_VERSION
from .portfolio import (
    PortfolioConfig,
    PortfolioResult,
    PredictionBar,
    simulate_portfolio,
)
from .race_config import (
    CandidateSpec,
    RaceConfig,
    RaceWindow,
    load_race_config,
    race_contract_sha256,
)
from .strategy_factors import build_candidate_scores
from .trading_constraints import load_trading_constraints

FOLD_TRADING_DAYS = 63
PURGE_TRADING_DAYS = 5


@dataclass(frozen=True)
class DecileCalibrator:
    boundaries: tuple[float, ...]
    expected_returns: tuple[float, ...]
    observations: int

    def predict(self, values: np.ndarray) -> np.ndarray:
        buckets = np.searchsorted(
            np.asarray(self.boundaries, dtype=float),
            values,
            side="right",
        )
        return np.asarray(self.expected_returns, dtype=float)[buckets]


@dataclass(frozen=True)
class WindowMetrics:
    trading_days: int
    total_return_pct: float
    annualized_return_pct: float
    sharpe: float
    max_drawdown_pct: float
    calmar: float
    cvar_5_pct: float
    turnover_pct: float
    average_hold_bars: float
    closed_trades: int


@dataclass(frozen=True)
class CandidateRaceResult:
    candidate_id: str
    family: str
    signal_frequency: str
    calibration_label: str
    selected_params: dict[str, float]
    validation_objective: float
    validation_fold_sharpes: tuple[float, ...]
    median_validation_fold_sharpe: float
    positive_validation_fold_share: float
    validation: WindowMetrics
    oos: WindowMetrics
    frozen: WindowMetrics
    double_cost_oos: WindowMetrics
    oos_fold_sharpes: tuple[float, ...]
    median_oos_fold_sharpe: float
    positive_oos_fold_share: float
    oos_upside_capture: float
    oos_downside_capture: float | None
    bootstrap_probability_sharpe_positive: float
    daily_gate_results: dict[str, bool]
    daily_gates_passed: bool


@dataclass(frozen=True)
class CandidatePredictionArtifact:
    candidate_id: str
    path: str
    sha256: str
    rows: int
    start_date: str
    end_date: str


@dataclass(frozen=True)
class _CandidateEvaluation:
    result: CandidateRaceResult
    predictions: pl.DataFrame


@dataclass(frozen=True)
class DailyRaceReport:
    schema_version: int
    status: str
    generated_at: str
    contract_schema: str
    contract_version: str
    contract_file: str
    contract_sha256: str
    feature_version: str
    panel_sha256: str
    panel_start: str
    panel_end: str
    complete_daily_trading_days: int
    data_quality_sha256: str
    historical_universe_end: str
    historical_universe: str
    historical_sw_industry_coverage: float
    latest_sw_industry_coverage: float
    fallback_risk_group_share: float
    candidates: tuple[CandidateRaceResult, ...]
    candidate_artifacts: tuple[CandidatePredictionArtifact, ...]
    minute_candidates: tuple[str, ...]
    selected_daily_candidate: str | None
    production_champion: str | None
    note: str


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _window_filter(window: RaceWindow) -> pl.Expr:
    expression = pl.col("date") >= window.start
    if window.end is not None:
        expression &= pl.col("date") <= window.end
    return expression


def load_instrument_intervals(path: Path | str) -> pl.DataFrame:
    rows: list[dict[str, Any]] = []
    for line in Path(path).read_text("utf-8").splitlines():
        if not line.strip():
            continue
        fields = line.split("\t")
        if len(fields) != 3:
            raise ValueError(f"invalid instrument interval row: {line!r}")
        instrument, start, end = fields
        rows.append(
            {
                "symbol": instrument.lower(),
                "__member_start": date.fromisoformat(start),
                "__member_end": date.fromisoformat(end),
            }
        )
    if not rows:
        raise ValueError("historical instrument membership is empty")
    intervals = pl.DataFrame(rows).sort("symbol", "__member_start")
    duplicate_starts = intervals.group_by("symbol", "__member_start").len().filter(
        pl.col("len") > 1
    )
    if not duplicate_starts.is_empty():
        raise ValueError("historical instrument membership has duplicate starts")
    return intervals


def filter_to_instrument_membership(
    frame: pl.DataFrame,
    intervals: pl.DataFrame,
) -> pl.DataFrame:
    required = {"date", "symbol"}
    if not required <= set(frame.columns):
        raise ValueError("membership filtering requires date and symbol")
    working = frame.sort("symbol", "date")
    joined = working.join_asof(
        intervals,
        left_on="date",
        right_on="__member_start",
        by="symbol",
        strategy="backward",
        check_sortedness=False,
    )
    return (
        joined.filter(pl.col("__member_start").is_not_null())
        .with_columns(
            (pl.col("date") <= pl.col("__member_end")).alias(
                "is_universe_member"
            ),
            pl.col("__member_end").alias("universe_member_end"),
        )
        .drop("__member_start", "__member_end")
    )


def attach_industry_metadata(
    frame: pl.DataFrame,
    membership_path: Path | str,
) -> pl.DataFrame:
    path = Path(membership_path)
    if not path.is_file():
        raise ValueError(
            "point-in-time SW industry membership is required for risk controls"
        )
    reference = pl.read_parquet(path)
    required = {"ts_code", "l1_code", "l1_name", "in_date", "out_date"}
    missing = required - set(reference.columns)
    if missing:
        raise ValueError(
            "SW industry membership missing columns: "
            f"{sorted(missing)}"
        )
    intervals = (
        reference.select(
            pl.col("ts_code")
            .cast(pl.String)
            .str.replace(r"\.SH$", "")
            .str.replace(r"\.SZ$", "")
            .str.replace(r"\.BJ$", "")
            .alias("__code"),
            pl.col("ts_code")
            .cast(pl.String)
            .str.extract(r"\.(SH|SZ|BJ)$", 1)
            .str.to_lowercase()
            .alias("__exchange"),
            pl.col("l1_code").cast(pl.String).alias("__industry_code"),
            pl.col("l1_name").cast(pl.String).alias("__industry_name"),
            pl.col("in_date")
            .cast(pl.String)
            .str.strptime(pl.Date, "%Y%m%d", strict=False)
            .alias("__industry_start"),
            pl.col("out_date")
            .cast(pl.String)
            .str.strptime(pl.Date, "%Y%m%d", strict=False)
            .alias("__industry_end"),
        )
        .with_columns(
            (pl.col("__exchange") + pl.col("__code")).alias("symbol"),
            (
                pl.col("__industry_code")
                + pl.lit(":")
                + pl.col("__industry_name")
            ).alias("__industry"),
        )
        .drop_nulls(["symbol", "__industry_start", "__industry"])
        .select(
            "symbol",
            "__industry_start",
            "__industry_end",
            "__industry",
        )
        .unique(
            ["symbol", "__industry_start", "__industry"],
            keep="last",
        )
        .sort("symbol", "__industry_start")
    )
    duplicate_starts = intervals.group_by("symbol", "__industry_start").len().filter(
        pl.col("len") > 1
    )
    if not duplicate_starts.is_empty():
        raise ValueError("SW industry membership has ambiguous interval starts")
    joined = frame.sort("symbol", "date").join_asof(
        intervals,
        left_on="date",
        right_on="__industry_start",
        by="symbol",
        strategy="backward",
        check_sortedness=False,
    )
    board = (
        pl.when(pl.col("symbol").str.starts_with("sh688"))
        .then(pl.lit("star"))
        .when(pl.col("symbol").str.starts_with("sz300"))
        .then(pl.lit("chinext"))
        .when(pl.col("symbol").str.starts_with("bj"))
        .then(pl.lit("beijing"))
        .otherwise(pl.lit("main"))
    )
    if "log_market_cap" in frame.columns:
        size_percentile = (
            pl.col("log_market_cap")
            .rank("average")
            .over("date")
            / pl.len().over("date")
        )
        size_bucket = (
            pl.when(pl.col("log_market_cap").is_null())
            .then(pl.lit("na"))
            .when(size_percentile <= 0.25)
            .then(pl.lit("q1"))
            .when(size_percentile <= 0.50)
            .then(pl.lit("q2"))
            .when(size_percentile <= 0.75)
            .then(pl.lit("q3"))
            .otherwise(pl.lit("q4"))
        )
    else:
        size_bucket = pl.lit("na")
    fallback = pl.concat_str(
        pl.lit("fallback:"),
        board,
        pl.lit(":size-"),
        size_bucket,
    )
    valid_industry = (
        pl.col("__industry_start").is_not_null()
        & (
            pl.col("__industry_end").is_null()
            | (pl.col("date") <= pl.col("__industry_end"))
        )
    )
    return joined.with_columns(
        pl.when(
            valid_industry
        )
        .then(pl.col("__industry"))
        .otherwise(fallback)
        .alias("theme"),
        pl.when(valid_industry)
        .then(pl.lit("sw-point-in-time"))
        .otherwise(pl.lit("board-size-fallback"))
        .alias("industry_source"),
    ).drop(
        "__industry_start",
        "__industry_end",
        "__industry",
    )


def fit_decile_calibrator(
    scored: pl.DataFrame,
    training_window: RaceWindow,
    deciles: int,
    minimum_observations_per_decile: int,
    label_column: str = "label_return_5",
) -> DecileCalibrator:
    if label_column not in scored.columns:
        raise ValueError(f"calibration label is missing: {label_column}")
    training = (
        scored.filter(_window_filter(training_window) & pl.col("eligible"))
        .select("raw_score", label_column)
        .drop_nulls()
    )
    if training.height < deciles * minimum_observations_per_decile:
        raise ValueError(
            "insufficient calibration observations: "
            f"{training.height} < {deciles * minimum_observations_per_decile}"
        )
    scores = training["raw_score"].to_numpy().astype(float)
    labels = training[label_column].to_numpy().astype(float)
    if not np.all(np.isfinite(scores)) or not np.all(np.isfinite(labels)):
        raise ValueError("calibration data contains non-finite values")
    quantiles = np.quantile(scores, np.linspace(0, 1, deciles + 1))
    boundaries = np.unique(quantiles[1:-1])
    if len(boundaries) != deciles - 1:
        raise ValueError("calibration scores do not span the requested deciles")
    buckets = np.searchsorted(boundaries, scores, side="right")
    means: list[float] = []
    centers: list[float] = []
    weights: list[int] = []
    for bucket in range(deciles):
        mask = buckets == bucket
        count = int(mask.sum())
        if count < minimum_observations_per_decile:
            raise ValueError(
                f"calibration decile {bucket} has only {count} observations"
            )
        means.append(float(labels[mask].mean()))
        centers.append(float(scores[mask].mean()))
        weights.append(count)
    isotonic = IsotonicRegression(increasing=True, out_of_bounds="clip")
    expected = isotonic.fit_transform(
        np.asarray(centers),
        np.asarray(means),
        sample_weight=np.asarray(weights),
    )
    return DecileCalibrator(
        boundaries=tuple(float(value) for value in boundaries),
        expected_returns=tuple(float(value) for value in expected),
        observations=training.height,
    )


def apply_calibrator(
    scored: pl.DataFrame,
    calibrator: DecileCalibrator,
) -> pl.DataFrame:
    values = scored["raw_score"].fill_null(float("nan")).to_numpy().astype(float)
    predictions = np.full(scored.height, -1.0, dtype=float)
    eligible = scored["eligible"].fill_null(False).to_numpy().astype(bool)
    valid = np.isfinite(values) & eligible
    predictions[valid] = calibrator.predict(values[valid])
    return scored.with_columns(pl.Series("prediction", predictions))


def _prediction_bars(frame: pl.DataFrame) -> list[PredictionBar]:
    required = {
        "date",
        "symbol",
        "raw_score",
        "prediction",
        "close",
        "next_trade_date",
        "next_raw_open",
        "next_raw_close",
        "adj_factor",
        "next_adj_factor",
        "next_can_buy",
        "next_can_sell",
        "amount",
        "volatility_20",
        "theme",
    }
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"race frame missing execution columns: {sorted(missing)}")
    complete = frame.drop_nulls(
        [
            "prediction",
            "close",
            "next_trade_date",
            "next_raw_open",
            "next_raw_close",
        ]
    )
    return [
        PredictionBar(
            decision_date=row["date"],
            trade_date=row["next_trade_date"],
            symbol=str(row["symbol"]),
            score=float(row["prediction"]),
            ranking_score=(
                float(row["raw_score"])
                if row["raw_score"] is not None
                else float(row["prediction"])
            ),
            close=float(row["close"]),
            next_open=float(row["next_raw_open"]),
            next_close=float(row["next_raw_close"]),
            adjustment_factor=float(row["adj_factor"]),
            next_adjustment_factor=float(row["next_adj_factor"]),
            can_buy=bool(row["next_can_buy"]),
            can_sell=bool(row["next_can_sell"]),
            liquidity_amount_yuan=(
                float(row["amount"]) if row["amount"] is not None else None
            ),
            volatility_20=(
                float(row["volatility_20"])
                if row["volatility_20"] is not None
                else None
            ),
            theme=str(row["theme"]),
        )
        for row in complete.iter_rows(named=True)
    ]


def _portfolio_config(
    config: RaceConfig,
    params: dict[str, float] | None = None,
) -> PortfolioConfig:
    params = params or {}
    return PortfolioConfig(
        start_cash=config.capital_yuan,
        min_expected_return=config.minimum_calibrated_net_return,
        switch_buffer=float(
            params.get(
                "ranking_switch_buffer",
                config.ranking_switch_buffer,
            )
        ),
        min_holding_bars=int(
            params.get(
                "minimum_holding_bars",
                load_trading_constraints().min_holding_bars,
            )
        ),
    )


def _simulate(
    predictions: pl.DataFrame,
    window: RaceWindow,
    config: RaceConfig,
    params: dict[str, float],
    cost_multiplier: float = 1.0,
) -> PortfolioResult:
    selected = predictions.filter(_window_filter(window))
    if window.end is not None:
        selected = selected.filter(pl.col("next_trade_date") <= window.end)
    return simulate_portfolio(
        _prediction_bars(selected),
        replace(
            _portfolio_config(config, params),
            cost_multiplier=cost_multiplier,
        ),
    )


def _window_metrics(result: PortfolioResult) -> WindowMetrics:
    observations = len(result.equity_curve)
    annualized_return = (
        (
            (1 + result.total_return_pct / 100) ** (252 / observations)
            - 1
        )
        * 100
        if observations > 0 and result.total_return_pct > -100
        else 0.0
    )
    calmar = annualized_return / max(abs(result.max_drawdown_pct), 0.01)
    return WindowMetrics(
        trading_days=observations,
        total_return_pct=result.total_return_pct,
        annualized_return_pct=annualized_return,
        sharpe=result.sharpe,
        max_drawdown_pct=result.max_drawdown_pct,
        calmar=calmar,
        cvar_5_pct=result.cvar_5_pct,
        turnover_pct=result.turnover_pct,
        average_hold_bars=result.average_hold_bars,
        closed_trades=result.closed_trades,
    )


def _validation_objective(
    result: PortfolioResult,
    minimum_closed_trades: int = 50,
) -> float:
    drawdown_penalty = max(0.0, abs(result.max_drawdown_pct) - 10.0) * 0.08
    turnover_penalty = max(0.0, result.turnover_pct - 2_000.0) / 2_000.0
    trade_penalty = (
        max(0, minimum_closed_trades - result.closed_trades)
        / minimum_closed_trades
        * 2
    )
    return result.sharpe - drawdown_penalty - turnover_penalty - trade_penalty


def _rolling_folds(
    dates: list[date],
    window: RaceWindow,
    fold_days: int = FOLD_TRADING_DAYS,
) -> tuple[RaceWindow, ...]:
    eligible = sorted(
        value for value in set(dates) if window.contains(value)
    )
    folds: list[RaceWindow] = []
    for offset in range(0, len(eligible), fold_days):
        values = eligible[offset : offset + fold_days]
        if len(values) < max(20, fold_days // 2):
            continue
        folds.append(RaceWindow(values[0], values[-1]))
    return tuple(folds)


def _training_window_before(
    all_dates: list[date],
    end_exclusive: date,
    purge_trading_days: int = PURGE_TRADING_DAYS,
) -> RaceWindow:
    eligible = sorted(value for value in set(all_dates) if value < end_exclusive)
    if len(eligible) <= purge_trading_days:
        raise ValueError("insufficient history before evaluation window")
    end = eligible[-purge_trading_days - 1]
    return RaceWindow(eligible[0], end)


def _purged_window(
    all_dates: list[date],
    window: RaceWindow,
    purge_trading_days: int,
) -> RaceWindow:
    eligible = sorted(
        value for value in set(all_dates) if window.contains(value)
    )
    if len(eligible) <= purge_trading_days:
        raise ValueError("insufficient history inside calibration window")
    return RaceWindow(
        eligible[0],
        eligible[-purge_trading_days - 1],
    )


def _walk_forward_predictions(
    scored: pl.DataFrame,
    folds: tuple[RaceWindow, ...],
    config: RaceConfig,
    candidate: CandidateSpec,
    params: dict[str, float],
) -> tuple[pl.DataFrame, tuple[float, ...]]:
    dates = scored["date"].unique().to_list()
    prediction_frames: list[pl.DataFrame] = []
    sharpes: list[float] = []
    for fold in folds:
        training = _training_window_before(
            dates,
            fold.start,
            candidate.label_horizon_bars,
        )
        calibrator = fit_decile_calibrator(
            scored,
            training,
            config.calibration_deciles,
            config.calibration_minimum_observations,
            candidate.calibration_label,
        )
        predicted = apply_calibrator(
            scored.filter(_window_filter(fold)),
            calibrator,
        )
        result = _simulate(predicted, fold, config, params)
        prediction_frames.append(predicted)
        sharpes.append(result.sharpe)
    if not prediction_frames:
        raise ValueError("walk-forward evaluation produced no folds")
    return pl.concat(prediction_frames, how="vertical"), tuple(sharpes)


def _daily_returns(result: PortfolioResult, start_cash: float) -> np.ndarray:
    equities = np.asarray(
        [start_cash, *[point.equity for point in result.equity_curve]],
        dtype=float,
    )
    return equities[1:] / equities[:-1] - 1


def _market_capture(
    result: PortfolioResult,
    scored: pl.DataFrame,
    start_cash: float,
) -> tuple[float, float | None]:
    strategy_returns = _daily_returns(result, start_cash)
    if strategy_returns.size == 0:
        return 0.0, None
    market_by_date = {
        row["date"]: float(row["market_return"])
        for row in scored.select("date", "market_return").unique("date").iter_rows(
            named=True
        )
        if row["market_return"] is not None
    }
    paired = [
        (strategy_return, market_by_date.get(point.date))
        for strategy_return, point in zip(
            strategy_returns,
            result.equity_curve,
            strict=True,
        )
    ]
    positive_pairs = [
        (strategy_return, market_return)
        for strategy_return, market_return in paired
        if market_return is not None and market_return > 0
    ]
    negative_pairs = [
        (strategy_return, market_return)
        for strategy_return, market_return in paired
        if market_return is not None and market_return < 0
    ]
    upside = 0.0
    if positive_pairs:
        strategy_mean = float(np.mean([value[0] for value in positive_pairs]))
        market_mean = float(np.mean([value[1] for value in positive_pairs]))
        upside = strategy_mean / market_mean if market_mean > 0 else 0.0
    downside = None
    if negative_pairs:
        strategy_mean = float(np.mean([value[0] for value in negative_pairs]))
        market_mean = float(np.mean([value[1] for value in negative_pairs]))
        downside = strategy_mean / market_mean if market_mean < 0 else None
    return upside, downside


def bootstrap_probability_sharpe_positive(
    result: PortfolioResult,
    start_cash: float,
    samples: int,
    block_days: int,
    seed: int,
) -> float:
    returns = _daily_returns(result, start_cash)
    if len(returns) < block_days or samples <= 0:
        return 0.0
    rng = np.random.default_rng(seed)
    starts = np.arange(0, len(returns) - block_days + 1)
    positive = 0
    blocks_needed = math.ceil(len(returns) / block_days)
    for _ in range(samples):
        selected_starts = rng.choice(starts, size=blocks_needed, replace=True)
        sample = np.concatenate(
            [returns[start : start + block_days] for start in selected_starts]
        )[: len(returns)]
        volatility = float(np.std(sample))
        sharpe = (
            float(np.mean(sample)) / volatility * math.sqrt(252)
            if volatility > 0
            else 0.0
        )
        positive += int(sharpe > 0)
    return positive / samples


def _select_params(
    frame: pl.DataFrame,
    candidate: CandidateSpec,
    config: RaceConfig,
) -> tuple[
    dict[str, float],
    float,
    PortfolioResult,
    pl.DataFrame,
    tuple[float, ...],
]:
    development = config.windows["development"]
    validation = config.windows["validation"]
    all_dates = frame["date"].unique().to_list()
    calibration_window = _purged_window(
        all_dates,
        development,
        candidate.label_horizon_bars,
    )
    validation_folds = _rolling_folds(
        all_dates,
        validation,
        config.validation_fold_trading_days,
    )
    if not validation_folds:
        raise ValueError("validation window produced no stability folds")
    best: tuple[
        tuple[float, ...],
        dict[str, float],
        float,
        PortfolioResult,
        pl.DataFrame,
        tuple[float, ...],
    ] | None = None
    for params in candidate.parameter_sets():
        scored = build_candidate_scores(frame, candidate.candidate_id, params)
        calibrator = fit_decile_calibrator(
            scored,
            calibration_window,
            config.calibration_deciles,
            config.calibration_minimum_observations,
            candidate.calibration_label,
        )
        predicted = apply_calibrator(scored, calibrator)
        result = _simulate(predicted, validation, config, params)
        fold_sharpes = tuple(
            _simulate(predicted, fold, config, params).sharpe
            for fold in validation_folds
        )
        positive_fold_share = (
            sum(value > 0 for value in fold_sharpes) / len(fold_sharpes)
        )
        median_fold_sharpe = float(median(fold_sharpes))
        objective = _validation_objective(
            result,
            config.promotion_gates.minimum_closed_trades,
        )
        rank = (
            positive_fold_share,
            median_fold_sharpe,
            objective,
            result.sharpe,
            -abs(result.max_drawdown_pct),
            -result.turnover_pct,
        )
        if best is None or rank > best[0]:
            best = (
                rank,
                params,
                objective,
                result,
                scored,
                fold_sharpes,
            )
    if best is None:
        raise ValueError(f"candidate {candidate.candidate_id} has no parameter set")
    return best[1], best[2], best[3], best[4], best[5]


def _evaluate_candidate(
    frame: pl.DataFrame,
    candidate: CandidateSpec,
    config: RaceConfig,
    data_quality_passed: bool,
) -> _CandidateEvaluation:
    (
        params,
        objective,
        validation_result,
        scored,
        validation_fold_sharpes,
    ) = _select_params(
        frame, candidate, config
    )
    all_dates = scored["date"].unique().to_list()
    validation_training = _purged_window(
        all_dates,
        config.windows["development"],
        candidate.label_horizon_bars,
    )
    validation_calibrator = fit_decile_calibrator(
        scored,
        validation_training,
        config.calibration_deciles,
        config.calibration_minimum_observations,
        candidate.calibration_label,
    )
    validation_predictions = apply_calibrator(
        scored.filter(_window_filter(config.windows["validation"])),
        validation_calibrator,
    ).with_columns(pl.lit("validation").alias("evaluation_stage"))
    oos_folds = _rolling_folds(
        all_dates,
        config.windows["oos"],
        config.oos_fold_trading_days,
    )
    oos_predictions, fold_sharpes = _walk_forward_predictions(
        scored,
        oos_folds,
        config,
        candidate,
        params,
    )
    oos_predictions = oos_predictions.with_columns(
        pl.lit("oos").alias("evaluation_stage")
    )
    oos_result = _simulate(
        oos_predictions,
        config.windows["oos"],
        config,
        params,
    )
    double_cost = _simulate(
        oos_predictions,
        config.windows["oos"],
        config,
        params,
        cost_multiplier=2.0,
    )

    frozen_training = _training_window_before(
        all_dates,
        config.windows["frozen"].start,
        candidate.label_horizon_bars,
    )
    frozen_calibrator = fit_decile_calibrator(
        scored,
        frozen_training,
        config.calibration_deciles,
        config.calibration_minimum_observations,
        candidate.calibration_label,
    )
    frozen_predictions = apply_calibrator(
        scored.filter(_window_filter(config.windows["frozen"])),
        frozen_calibrator,
    ).with_columns(pl.lit("frozen").alias("evaluation_stage"))
    frozen_result = _simulate(
        frozen_predictions,
        config.windows["frozen"],
        config,
        params,
    )
    upside_capture, downside_capture = _market_capture(
        oos_result,
        scored,
        config.capital_yuan,
    )
    bootstrap_probability = bootstrap_probability_sharpe_positive(
        oos_result,
        config.capital_yuan,
        config.bootstrap_samples,
        config.bootstrap_block_trading_days,
        config.bootstrap_seed,
    )
    gates = config.promotion_gates
    median_fold_sharpe = float(median(fold_sharpes))
    positive_fold_share = (
        sum(value > 0 for value in fold_sharpes) / len(fold_sharpes)
        if fold_sharpes
        else 0.0
    )
    validation_metrics = _window_metrics(validation_result)
    oos_metrics = _window_metrics(oos_result)
    frozen_metrics = _window_metrics(frozen_result)
    double_cost_metrics = _window_metrics(double_cost)
    gate_results = {
        "data_quality": data_quality_passed,
        "validation_trading_days": (
            validation_metrics.trading_days
            >= gates.minimum_validation_trading_days
        ),
        "oos_trading_days": (
            oos_metrics.trading_days >= gates.minimum_oos_trading_days
        ),
        "frozen_trading_days": (
            frozen_metrics.trading_days >= gates.minimum_frozen_trading_days
        ),
        "validation_sharpe": (
            validation_result.sharpe >= gates.minimum_validation_sharpe
        ),
        "validation_drawdown": (
            abs(validation_result.max_drawdown_pct)
            <= gates.maximum_drawdown_pct
        ),
        "oos_folds": len(fold_sharpes) >= gates.minimum_oos_folds,
        "closed_trades": oos_result.closed_trades
        >= gates.minimum_closed_trades,
        "oos_sharpe": oos_result.sharpe >= gates.minimum_oos_sharpe,
        "frozen_sharpe": frozen_result.sharpe >= gates.minimum_frozen_sharpe,
        "median_oos_fold_sharpe": (
            median_fold_sharpe >= gates.minimum_median_oos_fold_sharpe
        ),
        "positive_oos_fold_share": (
            positive_fold_share >= gates.minimum_positive_oos_fold_share
        ),
        "oos_annualized_return": (
            oos_metrics.annualized_return_pct
            >= gates.minimum_oos_annualized_return_pct
        ),
        "oos_calmar": oos_metrics.calmar >= gates.minimum_oos_calmar,
        "oos_drawdown": abs(oos_result.max_drawdown_pct)
        <= gates.maximum_drawdown_pct,
        "frozen_drawdown": abs(frozen_result.max_drawdown_pct)
        <= gates.maximum_drawdown_pct,
        "upside_capture": upside_capture >= gates.minimum_upside_capture,
        "downside_capture": (
            downside_capture is not None
            and downside_capture <= gates.maximum_downside_capture
        ),
        "bootstrap": bootstrap_probability
        >= gates.minimum_bootstrap_probability_sharpe_positive,
        "double_cost_positive": (
            double_cost.total_return_pct > 0
            if gates.double_cost_total_return_must_be_positive
            else True
        ),
        "double_cost_oos_sharpe": (
            double_cost.sharpe >= gates.minimum_double_cost_oos_sharpe
        ),
    }
    predictions = pl.concat(
        [
            validation_predictions,
            oos_predictions,
            frozen_predictions,
        ],
        how="vertical",
    )
    return _CandidateEvaluation(
        result=CandidateRaceResult(
            candidate_id=candidate.candidate_id,
            family=candidate.family,
            signal_frequency=candidate.signal_frequency,
            calibration_label=candidate.calibration_label,
            selected_params=params,
            validation_objective=objective,
            validation_fold_sharpes=validation_fold_sharpes,
            median_validation_fold_sharpe=float(
                median(validation_fold_sharpes)
            ),
            positive_validation_fold_share=(
                sum(value > 0 for value in validation_fold_sharpes)
                / len(validation_fold_sharpes)
            ),
            validation=validation_metrics,
            oos=oos_metrics,
            frozen=frozen_metrics,
            double_cost_oos=double_cost_metrics,
            oos_fold_sharpes=fold_sharpes,
            median_oos_fold_sharpe=median_fold_sharpe,
            positive_oos_fold_share=positive_fold_share,
            oos_upside_capture=upside_capture,
            oos_downside_capture=downside_capture,
            bootstrap_probability_sharpe_positive=bootstrap_probability,
            daily_gate_results=gate_results,
            daily_gates_passed=all(gate_results.values()),
        ),
        predictions=predictions,
    )


def evaluate_candidate(
    frame: pl.DataFrame,
    candidate: CandidateSpec,
    config: RaceConfig,
    data_quality_passed: bool,
) -> CandidateRaceResult:
    return _evaluate_candidate(
        frame,
        candidate,
        config,
        data_quality_passed,
    ).result


def select_daily_candidate(
    results: tuple[CandidateRaceResult, ...],
) -> str | None:
    passing = [result for result in results if result.daily_gates_passed]
    if not passing:
        return None
    passing.sort(
        key=lambda result: (
            result.median_oos_fold_sharpe,
            result.oos_upside_capture,
            (
                result.oos.total_return_pct
                / abs(result.oos.max_drawdown_pct)
                if result.oos.max_drawdown_pct
                else math.inf
            ),
            -result.oos.turnover_pct,
        ),
        reverse=True,
    )
    return passing[0].candidate_id


def _report_payload(report: DailyRaceReport) -> dict[str, Any]:
    return asdict(report)


_ARTIFACT_COLUMNS = (
    "evaluation_stage",
    "candidate_id",
    "date",
    "symbol",
    "ts_code",
    "is_universe_member",
    "eligible",
    "raw_score",
    "prediction",
    "close",
    "next_trade_date",
    "next_raw_open",
    "next_raw_close",
    "adj_factor",
    "next_adj_factor",
    "next_up_limit",
    "next_down_limit",
    "next_is_suspended",
    "next_can_buy",
    "next_can_sell",
    "amount",
    "volatility_20",
    "theme",
    "market_return",
)


def _write_candidate_artifact(
    frame: pl.DataFrame,
    candidate_id: str,
    directory: Path,
) -> CandidatePredictionArtifact:
    missing = set(_ARTIFACT_COLUMNS) - set(frame.columns)
    if missing:
        raise ValueError(
            f"candidate artifact missing columns: {sorted(missing)}"
        )
    artifact = frame.select(_ARTIFACT_COLUMNS).sort("date", "symbol")
    if artifact.is_empty():
        raise ValueError(f"candidate {candidate_id} produced no predictions")
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{candidate_id}.parquet"
    temporary = target.with_suffix(".parquet.tmp")
    artifact.write_parquet(temporary, compression="zstd")
    temporary.replace(target)
    return CandidatePredictionArtifact(
        candidate_id=candidate_id,
        path=str(target.resolve()),
        sha256=_file_sha256(target),
        rows=artifact.height,
        start_date=str(artifact["date"].min()),
        end_date=str(artifact["date"].max()),
    )


def _load_data_quality_evidence(
    panel_path: Path,
    panel: pl.DataFrame | None,
    config: RaceConfig,
    quality_path: Path,
) -> tuple[int, str]:
    manifest_path = panel_path.with_suffix(".manifest.json")
    if not manifest_path.is_file():
        raise ValueError("feature manifest is required for the production race")
    manifest = json.loads(manifest_path.read_text("utf-8"))
    if manifest.get("feature_version") != FEATURE_VERSION:
        raise ValueError(
            "feature manifest is stale: "
            f"{manifest.get('feature_version')} != {FEATURE_VERSION}"
        )
    if panel is None:
        statistics = (
            pl.scan_parquet(panel_path)
            .select(
                pl.len().alias("rows"),
                pl.col("date").min().alias("start"),
                pl.col("date").max().alias("end"),
                pl.col("date").n_unique().alias("unique_dates"),
            )
            .collect()
        )
        panel_rows = int(statistics["rows"][0])
        panel_start = statistics["start"][0]
        panel_end = statistics["end"][0]
        unique_dates = int(statistics["unique_dates"][0])
    else:
        dates = panel["date"]
        panel_rows = panel.height
        panel_start = dates.min()
        panel_end = dates.max()
        unique_dates = dates.n_unique()
    if (
        manifest.get("rows") != panel_rows
        or manifest.get("start_date") != str(panel_start)
        or manifest.get("end_date") != str(panel_end)
        or manifest.get("as_of_date") != str(panel_end)
    ):
        raise ValueError("feature manifest does not describe the supplied panel")

    if not quality_path.is_file():
        raise ValueError("feature quality evidence is required for the production race")
    quality = json.loads(quality_path.read_text("utf-8"))
    if (
        quality.get("passed") is not True
        or quality.get("rows") != panel_rows
        or quality.get("duplicate_keys") != 0
        or quality.get("future_rows") != 0
        or quality.get("failures")
    ):
        raise ValueError("feature quality evidence did not pass")

    coverage = assert_production_dataset(
        panel_path.parents[1] / "data",
        str(panel_start),
        str(panel_end),
        config.minimum_complete_daily_trading_days,
    )
    if unique_dates < config.minimum_complete_daily_trading_days:
        raise ValueError(
            f"panel has only {unique_dates} trading days; "
            f"requires {config.minimum_complete_daily_trading_days}"
        )
    evidence_hash = hashlib.sha256()
    for path in (manifest_path, quality_path):
        evidence_hash.update(path.name.encode())
        evidence_hash.update(path.read_bytes())
    evidence_hash.update(
        json.dumps(asdict(coverage), sort_keys=True, default=str).encode()
    )
    return coverage.common_required_days, evidence_hash.hexdigest()


def run_daily_race(
    panel_path: Path | str,
    config_path: Path | str,
    output_path: Path | str,
    industry_membership_path: Path | str | None = None,
    quality_path: Path | str | None = None,
) -> DailyRaceReport:
    panel_path = Path(panel_path)
    output = Path(output_path)
    config = load_race_config(config_path)
    intervals = load_instrument_intervals(config.historical_universe_file)
    historical_symbols = intervals["symbol"].unique().to_list()
    schema = set(pl.scan_parquet(panel_path).collect_schema().names())
    required = {
        "date",
        "symbol",
        "next_raw_open",
        "next_raw_close",
        "adj_factor",
        "next_adj_factor",
        "next_up_limit",
        "next_down_limit",
        "next_is_suspended",
        "position_60",
        "drawdown_60",
        *(
            candidate.calibration_label
            for candidate in config.candidates
        ),
    }
    missing = required - schema
    if missing:
        raise ValueError(
            "feature panel must be rebuilt for production race: "
            f"missing {sorted(missing)}"
        )
    statistics = (
        pl.scan_parquet(panel_path)
        .select(
            pl.col("date").min().alias("start"),
            pl.col("date").max().alias("end"),
        )
        .collect()
    )
    panel_start = statistics["start"][0]
    panel_end = statistics["end"][0]
    development_end = config.windows["development"].end
    if (
        panel_start is None
        or development_end is None
        or panel_start > development_end
        or panel_end is None
        or panel_end < config.windows["frozen"].start
    ):
        raise ValueError("feature panel does not cover the preregistered windows")
    quality_evidence = (
        Path(quality_path)
        if quality_path is not None
        else panel_path.parents[1] / "quality" / "feature-panel.json"
    )
    complete_days, quality_sha256 = _load_data_quality_evidence(
        panel_path,
        None,
        config,
        quality_evidence,
    )
    panel = (
        pl.scan_parquet(panel_path)
        .filter(pl.col("symbol").is_in(historical_symbols))
        .collect()
    )
    race_frame = filter_to_instrument_membership(panel, intervals)
    historical_universe_end = race_frame.filter("is_universe_member")["date"].max()
    if historical_universe_end is None or historical_universe_end < panel_end:
        raise ValueError(
            "historical universe membership is stale: "
            f"{historical_universe_end} < {panel_end}"
        )
    reference = (
        Path(industry_membership_path)
        if industry_membership_path is not None
        else (
            panel_path.parents[1]
            / "data"
            / "reference"
            / "sw_industry_membership.parquet"
        )
    )
    race_frame = attach_industry_metadata(race_frame, reference)
    active_members = race_frame.filter("is_universe_member")
    risk_group_coverage = (
        1 - active_members["theme"].null_count() / max(active_members.height, 1)
    )
    if risk_group_coverage < 1:
        raise ValueError(
            "point-in-time risk group coverage is incomplete: "
            f"{risk_group_coverage:.2%}"
        )
    historical_sw_coverage = active_members.filter(
        pl.col("industry_source") == "sw-point-in-time"
    ).height / max(active_members.height, 1)
    latest_date = active_members["date"].max()
    latest_members = active_members.filter(pl.col("date") == latest_date)
    latest_sw_coverage = latest_members.filter(
        pl.col("industry_source") == "sw-point-in-time"
    ).height / max(latest_members.height, 1)
    if latest_sw_coverage < 0.95:
        raise ValueError(
            "latest point-in-time SW industry coverage below 95%: "
            f"{latest_sw_coverage:.2%}"
        )
    evaluations = tuple(
        _evaluate_candidate(
            race_frame,
            candidate,
            config,
            True,
        )
        for candidate in config.candidates
    )
    candidates = tuple(evaluation.result for evaluation in evaluations)
    artifact_directory = output.parent / (
        "daily-candidates-v3"
        if config.schema == "ashare-production-race/v2"
        else "daily-candidates"
    )
    candidate_artifacts = tuple(
        _write_candidate_artifact(
            evaluation.predictions,
            evaluation.result.candidate_id,
            artifact_directory,
        )
        for evaluation in evaluations
    )
    selected = select_daily_candidate(candidates)
    minute_candidates = tuple(
        candidate.candidate_id
        for candidate in config.candidates
        if candidate.requires_minute
    )
    if minute_candidates:
        status = "daily_phase_complete_pending_minute_race"
        production_champion = None
        note = (
            "Daily evaluation is complete. Candidates that explicitly depend "
            "on 5-minute execution remain blocked until their own minute data "
            "and integrated gates pass."
        )
    elif selected is not None:
        status = "production_champion_selected"
        production_champion = selected
        note = (
            "All candidates are daily-only. The sole daily candidate that "
            "wins among all gate-passing candidates is the production "
            "champion; minute coverage is not part of this contract."
        )
    else:
        status = "no_production_champion"
        production_champion = None
        note = (
            "No daily-only candidate passed every preregistered gate. "
            "Production must remain cash-only and the final period must not "
            "be reused for parameter tuning."
        )
    report = DailyRaceReport(
        schema_version=(
            3 if config.schema == "ashare-production-race/v2" else 2
        ),
        status=status,
        generated_at=datetime.now(UTC).isoformat(),
        contract_schema=config.schema,
        contract_version=config.version,
        contract_file=config.source_path.name,
        contract_sha256=race_contract_sha256(config),
        feature_version=FEATURE_VERSION,
        panel_sha256=_file_sha256(panel_path),
        panel_start=str(panel_start),
        panel_end=str(panel_end),
        complete_daily_trading_days=complete_days,
        data_quality_sha256=quality_sha256,
        historical_universe_end=str(historical_universe_end),
        historical_universe="csi800-point-in-time",
        historical_sw_industry_coverage=historical_sw_coverage,
        latest_sw_industry_coverage=latest_sw_coverage,
        fallback_risk_group_share=1 - historical_sw_coverage,
        candidates=candidates,
        candidate_artifacts=candidate_artifacts,
        minute_candidates=minute_candidates,
        selected_daily_candidate=selected,
        production_champion=production_champion,
        note=note,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(
        json.dumps(_report_payload(report), ensure_ascii=False, indent=2) + "\n",
        "utf-8",
    )
    temporary.replace(output)
    return report
