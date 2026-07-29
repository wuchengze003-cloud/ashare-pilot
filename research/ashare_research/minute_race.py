"""Final three-candidate race with integrated daily and 5-minute execution."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, replace
from datetime import UTC, date, datetime
from pathlib import Path
from statistics import median
from typing import Any

import polars as pl

from .minute_portfolio import (
    MinuteRequirement,
    ParquetMinuteStore,
    collect_minute_requirements,
    prediction_bars_from_artifact,
    requirement_ranges,
    simulate_minute_portfolio,
)
from .portfolio import PortfolioConfig, PortfolioResult
from .race_config import (
    CandidateSpec,
    RaceConfig,
    load_race_config,
    race_contract_sha256,
)
from .strategy_race import (
    WindowMetrics,
    _market_capture,
    _rolling_folds,
    _validation_objective,
    _window_filter,
    _window_metrics,
    bootstrap_probability_sharpe_positive,
)


@dataclass(frozen=True)
class MinuteCandidateRaceResult:
    candidate_id: str
    family: str
    daily_params: dict[str, float]
    minute_params: dict[str, float]
    validation_objective: float
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
    daily_oos_sharpe: float
    integrated_gate_results: dict[str, bool]
    integrated_gates_passed: bool


@dataclass(frozen=True)
class MinuteRaceReport:
    schema_version: int
    status: str
    generated_at: str
    contract_sha256: str
    daily_report_sha256: str
    requirement_manifest_sha256: str
    minute_data_sha256: str | None
    required_symbol_days: int
    required_symbols: int
    available_symbol_days: int
    available_symbols: int
    minute_coverage_pct: float
    missing_symbol_days: int
    missing_preview: tuple[str, ...]
    candidates: tuple[MinuteCandidateRaceResult, ...]
    production_champion: str | None
    note: str


@dataclass(frozen=True)
class MinuteCoverageAudit:
    required_symbol_days: int
    required_symbols: int
    available_symbol_days: int
    available_symbols: int
    coverage_pct: float
    missing_symbol_days: int
    missing_preview: tuple[str, ...]
    data_sha256: str | None

    @property
    def passed(self) -> bool:
        return self.missing_symbol_days == 0


@dataclass(frozen=True)
class _Evaluation:
    result: MinuteCandidateRaceResult
    oos_result: PortfolioResult
    frozen_result: PortfolioResult


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
        "utf-8",
    )
    temporary.replace(path)


def _load_daily_report(path: Path, config: RaceConfig) -> dict[str, Any]:
    payload = json.loads(path.read_text("utf-8"))
    if payload.get("schema_version") != 2:
        raise ValueError("daily race report must use schema version 2")
    if payload.get("contract_sha256") != race_contract_sha256(config):
        raise ValueError("daily race report contract does not match current config")
    artifacts = payload.get("candidate_artifacts")
    if not isinstance(artifacts, list) or len(artifacts) != 3:
        raise ValueError("daily race report must contain three candidate artifacts")
    expected = {candidate.candidate_id for candidate in config.candidates}
    if set(payload.get("minute_candidates") or []) != expected:
        raise ValueError("all three candidates must enter the minute race")
    for artifact in artifacts:
        artifact_path = Path(str(artifact["path"]))
        if not artifact_path.is_file():
            raise ValueError(f"daily candidate artifact is missing: {artifact_path}")
        if _file_sha256(artifact_path) != artifact.get("sha256"):
            raise ValueError(
                f"daily candidate artifact hash mismatch: {artifact_path}"
            )
    return payload


def _candidate_maps(
    daily_report: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], dict[str, Path]]:
    daily = {
        str(candidate["candidate_id"]): candidate
        for candidate in daily_report["candidates"]
    }
    artifacts = {
        str(artifact["candidate_id"]): Path(str(artifact["path"]))
        for artifact in daily_report["candidate_artifacts"]
    }
    return daily, artifacts


def build_minute_requirement_manifest(
    daily_report_path: Path | str,
    config_path: Path | str,
    output_path: Path | str,
) -> dict[str, Any]:
    config = load_race_config(config_path)
    daily_path = Path(daily_report_path)
    daily_report = _load_daily_report(daily_path, config)
    daily, artifacts = _candidate_maps(daily_report)
    by_candidate: dict[str, list[MinuteRequirement]] = {}
    union: set[MinuteRequirement] = set()
    for candidate in config.candidates:
        artifact = pl.read_parquet(artifacts[candidate.candidate_id])
        daily_params = daily[candidate.candidate_id]["selected_params"]
        requirements = list(
            collect_minute_requirements(
                prediction_bars_from_artifact(artifact),
                PortfolioConfig(
                    start_cash=config.capital_yuan,
                    min_expected_return=(
                        config.minimum_calibrated_net_return
                    ),
                    switch_buffer=float(
                        daily_params.get(
                            "ranking_switch_buffer",
                            config.ranking_switch_buffer,
                        )
                    ),
                ),
            )
        )
        by_candidate[candidate.candidate_id] = requirements
        union.update(requirements)
    ordered = sorted(union, key=lambda item: (item.trade_date, item.symbol))
    payload = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "contract_sha256": race_contract_sha256(config),
        "daily_report_path": str(daily_path.resolve()),
        "daily_report_sha256": _file_sha256(daily_path),
        "candidate_counts": {
            candidate_id: len(requirements)
            for candidate_id, requirements in by_candidate.items()
        },
        "required_symbol_days": len(ordered),
        "required_symbols": len({item.symbol for item in ordered}),
        "requirements": [
            {
                "symbol": item.symbol,
                "trade_date": str(item.trade_date),
            }
            for item in ordered
        ],
        "symbol_ranges": requirement_ranges(ordered),
    }
    _atomic_json(Path(output_path), payload)
    return payload


def _load_requirement_manifest(
    path: Path,
    daily_report_path: Path,
    config: RaceConfig,
) -> tuple[dict[str, Any], tuple[MinuteRequirement, ...]]:
    payload = json.loads(path.read_text("utf-8"))
    if payload.get("schema_version") != 1:
        raise ValueError("unsupported minute requirement manifest")
    if payload.get("contract_sha256") != race_contract_sha256(config):
        raise ValueError("minute requirements use a stale race contract")
    if payload.get("daily_report_sha256") != _file_sha256(daily_report_path):
        raise ValueError("minute requirements do not match the daily report")
    rows = payload.get("requirements")
    if not isinstance(rows, list) or not rows:
        raise ValueError("minute requirement manifest is empty")
    requirements = tuple(
        MinuteRequirement(
            symbol=str(row["symbol"]),
            trade_date=date.fromisoformat(str(row["trade_date"])),
        )
        for row in rows
    )
    if len(set(requirements)) != len(requirements):
        raise ValueError("minute requirement manifest contains duplicates")
    if payload.get("required_symbol_days") != len(requirements):
        raise ValueError("minute requirement manifest count mismatch")
    return payload, requirements


def _audit_required_data(
    requirements: tuple[MinuteRequirement, ...],
    store: ParquetMinuteStore,
) -> MinuteCoverageAudit:
    digest = hashlib.sha256()
    missing: list[MinuteRequirement] = []
    available: list[MinuteRequirement] = []
    for item in requirements:
        frame = store(item.symbol, item.trade_date)
        compact = item.trade_date.strftime("%Y%m%d")
        selected = (
            frame.filter(pl.col("trade_date").cast(pl.String) == compact)
            if not frame.is_empty() and "trade_date" in frame.columns
            else pl.DataFrame()
        )
        if selected.height != 48:
            missing.append(item)
            continue
        available.append(item)
        digest.update(item.symbol.encode())
        digest.update(compact.encode())
        digest.update(
            selected.sort("trade_time")
            .select(
                "trade_time",
                "open",
                "high",
                "low",
                "close",
                "volume",
                "amount",
            )
            .hash_rows(seed=20260728)
            .to_numpy()
            .tobytes()
        )
    required_count = len(requirements)
    available_count = len(available)
    return MinuteCoverageAudit(
        required_symbol_days=required_count,
        required_symbols=len({item.symbol for item in requirements}),
        available_symbol_days=available_count,
        available_symbols=len({item.symbol for item in available}),
        coverage_pct=(
            available_count / required_count * 100
            if required_count
            else 0.0
        ),
        missing_symbol_days=len(missing),
        missing_preview=tuple(
            f"{item.symbol}@{item.trade_date}" for item in missing[:10]
        ),
        data_sha256=digest.hexdigest() if not missing else None,
    )


def _simulate(
    artifact: pl.DataFrame,
    candidate_id: str,
    params: dict[str, float],
    daily_params: dict[str, float],
    store: ParquetMinuteStore,
    config: RaceConfig,
    cost_multiplier: float = 1.0,
) -> PortfolioResult:
    portfolio_config = replace(
        PortfolioConfig(
            start_cash=config.capital_yuan,
            min_expected_return=config.minimum_calibrated_net_return,
            switch_buffer=float(
                daily_params.get(
                    "ranking_switch_buffer",
                    config.ranking_switch_buffer,
                )
            ),
        ),
        cost_multiplier=cost_multiplier,
    )
    return simulate_minute_portfolio(
        artifact,
        candidate_id,
        params,
        store,
        portfolio_config,
    )


def _select_minute_params(
    validation: pl.DataFrame,
    candidate: CandidateSpec,
    daily_params: dict[str, float],
    store: ParquetMinuteStore,
    config: RaceConfig,
) -> tuple[dict[str, float], float, PortfolioResult]:
    best: tuple[
        tuple[float, float, float, float],
        dict[str, float],
        PortfolioResult,
    ] | None = None
    for params in candidate.minute_parameter_sets():
        result = _simulate(
            validation,
            candidate.candidate_id,
            params,
            daily_params,
            store,
            config,
        )
        objective = _validation_objective(
            result,
            config.promotion_gates.minimum_closed_trades,
        )
        rank = (
            objective,
            result.sharpe,
            -abs(result.max_drawdown_pct),
            -result.turnover_pct,
        )
        if best is None or rank > best[0]:
            best = (rank, params, result)
    if best is None:
        raise ValueError(
            f"candidate {candidate.candidate_id} has no minute parameter set"
        )
    return best[1], best[0][0], best[2]


def _fold_sharpes(
    artifact: pl.DataFrame,
    candidate_id: str,
    params: dict[str, float],
    daily_params: dict[str, float],
    store: ParquetMinuteStore,
    config: RaceConfig,
) -> tuple[float, ...]:
    folds = _rolling_folds(
        artifact["date"].unique().to_list(),
        config.windows["oos"],
    )
    return tuple(
        _simulate(
            artifact.filter(_window_filter(fold)),
            candidate_id,
            params,
            daily_params,
            store,
            config,
        ).sharpe
        for fold in folds
    )


def _evaluate_candidate(
    artifact: pl.DataFrame,
    candidate: CandidateSpec,
    daily_result: dict[str, Any],
    store: ParquetMinuteStore,
    config: RaceConfig,
) -> _Evaluation:
    validation_frame = artifact.filter(
        pl.col("evaluation_stage") == "validation"
    )
    oos_frame = artifact.filter(pl.col("evaluation_stage") == "oos")
    frozen_frame = artifact.filter(pl.col("evaluation_stage") == "frozen")
    daily_params = {
        str(key): float(value)
        for key, value in daily_result["selected_params"].items()
    }
    params, objective, validation_result = _select_minute_params(
        validation_frame,
        candidate,
        daily_params,
        store,
        config,
    )
    oos_result = _simulate(
        oos_frame,
        candidate.candidate_id,
        params,
        daily_params,
        store,
        config,
    )
    frozen_result = _simulate(
        frozen_frame,
        candidate.candidate_id,
        params,
        daily_params,
        store,
        config,
    )
    double_cost = _simulate(
        oos_frame,
        candidate.candidate_id,
        params,
        daily_params,
        store,
        config,
        cost_multiplier=2.0,
    )
    fold_sharpes = _fold_sharpes(
        oos_frame,
        candidate.candidate_id,
        params,
        daily_params,
        store,
        config,
    )
    median_fold = float(median(fold_sharpes))
    positive_fold_share = (
        sum(value > 0 for value in fold_sharpes) / len(fold_sharpes)
        if fold_sharpes
        else 0.0
    )
    upside, downside = _market_capture(
        oos_result,
        oos_frame,
        config.capital_yuan,
    )
    bootstrap = bootstrap_probability_sharpe_positive(
        oos_result,
        config.capital_yuan,
        config.bootstrap_samples,
        config.bootstrap_block_trading_days,
        config.bootstrap_seed,
    )
    validation_metrics = _window_metrics(validation_result)
    oos_metrics = _window_metrics(oos_result)
    frozen_metrics = _window_metrics(frozen_result)
    double_cost_metrics = _window_metrics(double_cost)
    gates = config.promotion_gates
    daily_oos_sharpe = float(daily_result["oos"]["sharpe"])
    gate_results = {
        "data_quality": True,
        "validation_sharpe": (
            validation_result.sharpe >= gates.minimum_validation_sharpe
        ),
        "validation_drawdown": (
            abs(validation_result.max_drawdown_pct)
            <= gates.maximum_drawdown_pct
        ),
        "oos_folds": len(fold_sharpes) >= gates.minimum_oos_folds,
        "closed_trades": (
            oos_result.closed_trades >= gates.minimum_closed_trades
        ),
        "oos_sharpe": oos_result.sharpe >= gates.minimum_oos_sharpe,
        "frozen_sharpe": (
            frozen_result.sharpe >= gates.minimum_frozen_sharpe
        ),
        "median_oos_fold_sharpe": (
            median_fold >= gates.minimum_median_oos_fold_sharpe
        ),
        "positive_oos_fold_share": (
            positive_fold_share >= gates.minimum_positive_oos_fold_share
        ),
        "oos_annualized_return": (
            oos_metrics.annualized_return_pct
            >= gates.minimum_oos_annualized_return_pct
        ),
        "oos_calmar": oos_metrics.calmar >= gates.minimum_oos_calmar,
        "oos_drawdown": (
            abs(oos_result.max_drawdown_pct) <= gates.maximum_drawdown_pct
        ),
        "frozen_drawdown": (
            abs(frozen_result.max_drawdown_pct)
            <= gates.maximum_drawdown_pct
        ),
        "upside_capture": upside >= gates.minimum_upside_capture,
        "downside_capture": (
            downside is not None
            and downside <= gates.maximum_downside_capture
        ),
        "bootstrap": (
            bootstrap
            >= gates.minimum_bootstrap_probability_sharpe_positive
        ),
        "double_cost_positive": (
            double_cost.total_return_pct > 0
            if gates.double_cost_total_return_must_be_positive
            else True
        ),
        "double_cost_oos_sharpe": (
            double_cost.sharpe >= gates.minimum_double_cost_oos_sharpe
        ),
        "minute_non_degrading": (
            oos_result.sharpe >= daily_oos_sharpe
            if gates.minute_overlay_must_not_reduce_sharpe
            else True
        ),
    }
    result = MinuteCandidateRaceResult(
        candidate_id=candidate.candidate_id,
        family=candidate.family,
        daily_params={
            str(key): float(value)
            for key, value in daily_result["selected_params"].items()
        },
        minute_params=params,
        validation_objective=objective,
        validation=validation_metrics,
        oos=oos_metrics,
        frozen=frozen_metrics,
        double_cost_oos=double_cost_metrics,
        oos_fold_sharpes=fold_sharpes,
        median_oos_fold_sharpe=median_fold,
        positive_oos_fold_share=positive_fold_share,
        oos_upside_capture=upside,
        oos_downside_capture=downside,
        bootstrap_probability_sharpe_positive=bootstrap,
        daily_oos_sharpe=daily_oos_sharpe,
        integrated_gate_results=gate_results,
        integrated_gates_passed=all(gate_results.values()),
    )
    return _Evaluation(result, oos_result, frozen_result)


def select_production_champion(
    candidates: tuple[MinuteCandidateRaceResult, ...],
) -> str | None:
    passing = [
        candidate for candidate in candidates if candidate.integrated_gates_passed
    ]
    if not passing:
        return None
    passing.sort(
        key=lambda candidate: (
            candidate.median_oos_fold_sharpe,
            candidate.oos_upside_capture,
            candidate.oos.calmar,
            -candidate.oos.turnover_pct,
            candidate.candidate_id,
        ),
        reverse=True,
    )
    return passing[0].candidate_id


def _write_evidence(
    directory: Path,
    candidate_id: str,
    stage: str,
    result: PortfolioResult,
) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    curve = pl.DataFrame(
        [
            {
                "date": point.date,
                "equity": point.equity,
                "cash": point.cash,
                "positions": point.positions,
            }
            for point in result.equity_curve
        ]
    )
    trades = pl.DataFrame(
        [
            {
                **asdict(trade),
                "decision_date": str(trade.decision_date),
                "trade_date": str(trade.trade_date),
            }
            for trade in result.trades
        ],
        schema={
            "decision_date": pl.String,
            "trade_date": pl.String,
            "symbol": pl.String,
            "side": pl.String,
            "amount": pl.Float64,
            "fee": pl.Float64,
            "reason": pl.String,
            "shares": pl.Float64,
            "price": pl.Float64,
            "effective_price": pl.Float64,
            "commission": pl.Float64,
            "stamp_duty": pl.Float64,
            "slippage": pl.Float64,
            "impact_bps": pl.Float64,
            "net_cash_flow": pl.Float64,
            "execution_time": pl.String,
        },
        strict=False,
    )
    for label, frame in (("equity", curve), ("trades", trades)):
        target = directory / f"{candidate_id}-{stage}-{label}.parquet"
        temporary = target.with_suffix(".parquet.tmp")
        frame.write_parquet(temporary, compression="zstd")
        temporary.replace(target)


def run_minute_race(
    daily_report_path: Path | str,
    config_path: Path | str,
    minute_root: Path | str,
    requirement_manifest_path: Path | str,
    output_path: Path | str,
) -> MinuteRaceReport:
    config = load_race_config(config_path)
    daily_path = Path(daily_report_path)
    daily_report = _load_daily_report(daily_path, config)
    daily_results, artifact_paths = _candidate_maps(daily_report)
    requirement_path = Path(requirement_manifest_path)
    requirement_payload, requirements = _load_requirement_manifest(
        requirement_path,
        daily_path,
        config,
    )
    store = ParquetMinuteStore(minute_root)
    coverage = _audit_required_data(requirements, store)
    output = Path(output_path)
    if not coverage.passed:
        report = MinuteRaceReport(
            schema_version=2,
            status="blocked_minute_data_coverage",
            generated_at=datetime.now(UTC).isoformat(),
            contract_sha256=race_contract_sha256(config),
            daily_report_sha256=_file_sha256(daily_path),
            requirement_manifest_sha256=_file_sha256(requirement_path),
            minute_data_sha256=None,
            required_symbol_days=coverage.required_symbol_days,
            required_symbols=coverage.required_symbols,
            available_symbol_days=coverage.available_symbol_days,
            available_symbols=coverage.available_symbols,
            minute_coverage_pct=coverage.coverage_pct,
            missing_symbol_days=coverage.missing_symbol_days,
            missing_preview=coverage.missing_preview,
            candidates=(),
            production_champion=None,
            note=(
                "Minute execution research is fail-closed because the local "
                "data package does not cover every pre-registered symbol-day. "
                "No daily fallback, historical universe backfill, or gate "
                "relaxation is permitted."
            ),
        )
        _atomic_json(output, asdict(report))
        return report
    evaluations = tuple(
        _evaluate_candidate(
            pl.read_parquet(artifact_paths[candidate.candidate_id]),
            candidate,
            daily_results[candidate.candidate_id],
            store,
            config,
        )
        for candidate in config.candidates
    )
    candidates = tuple(evaluation.result for evaluation in evaluations)
    champion = select_production_champion(candidates)
    evidence_directory = output.parent / "minute-evidence"
    for evaluation in evaluations:
        _write_evidence(
            evidence_directory,
            evaluation.result.candidate_id,
            "oos",
            evaluation.oos_result,
        )
        _write_evidence(
            evidence_directory,
            evaluation.result.candidate_id,
            "frozen",
            evaluation.frozen_result,
        )
    report = MinuteRaceReport(
        schema_version=2,
        status=(
            "production_champion_selected"
            if champion
            else "no_candidate_passed_production_gates"
        ),
        generated_at=datetime.now(UTC).isoformat(),
        contract_sha256=race_contract_sha256(config),
        daily_report_sha256=_file_sha256(daily_path),
        requirement_manifest_sha256=_file_sha256(requirement_path),
        minute_data_sha256=coverage.data_sha256,
        required_symbol_days=coverage.required_symbol_days,
        required_symbols=coverage.required_symbols,
        available_symbol_days=coverage.available_symbol_days,
        available_symbols=coverage.available_symbols,
        minute_coverage_pct=coverage.coverage_pct,
        missing_symbol_days=coverage.missing_symbol_days,
        missing_preview=coverage.missing_preview,
        candidates=candidates,
        production_champion=champion,
        note=(
            "Only the integrated daily-plus-minute result can become the "
            "single production champion. No candidate is promoted when any "
            "pre-registered data, drawdown, significance, cost, or upside "
            "participation gate fails."
        ),
    )
    _atomic_json(output, asdict(report))
    return report
