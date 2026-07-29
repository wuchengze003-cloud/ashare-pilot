"""Command line control plane for research and shadow inference."""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path

import polars as pl

from .assessment import assess_registered_challenger
from .baseline import export_v1_baseline
from .candidate import (
    build_model_manifest,
    build_promotion_evidence,
    write_model_manifest,
    write_promotion_evidence,
)
from .contracts import ModelManifest, ModelMetrics, PriceBar, PromotionEvidence
from .cost_config import load_cost_model
from .data_sync import (
    sync_csi800_membership,
    sync_sw_industry_membership,
    sync_tushare,
)
from .evaluation import evaluate_oos_predictions, write_evaluation_report
from .experiment import run_challenger_experiment
from .features import build_feature_panel
from .health import build_model_health, write_model_health
from .inference import generate_shadow_snapshot
from .ledger import (
    backfill_outcomes,
    init_ledger,
    ledger_counts,
    read_predictions,
    summarize_outcomes,
)
from .minute_data import (
    _symbol_to_ts_code,
    load_daily_volume_map,
    load_suspended_map,
    load_trading_dates,
    probe_minute_data,
    sync_minute_data,
)
from .minute_quality import run_minute_quality
from .minute_race import (
    build_minute_requirement_manifest,
    run_minute_race,
)
from .monitoring import ModelHealth, rollback_reasons
from .portfolio import PortfolioConfig
from .promotion import evaluate_promotion
from .qlib_benchmark import run_alpha158_benchmark
from .qlib_bootstrap import bootstrap_qlib_dataset, validate_qlib_dataset
from .quality import run_evidently_drift, validate_feature_panel, write_quality_result
from .rebound_report import create_config_lock, run_rebound_study, verify_config_lock
from .rebound_study import load_universe_membership
from .registry import (
    load_registry,
    promote,
    register_candidate,
    rollback,
    verify_model_artifacts,
    verify_promotion_evidence,
)
from .shadow_evaluation import evaluate_shadow_account, write_shadow_evaluation
from .strategy_race import run_daily_race
from .training import train_models

RESEARCH_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RUNTIME = RESEARCH_ROOT / "runtime"


def _round_trip_fee_bps(legacy_side_fee_bps: float | None) -> float:
    if legacy_side_fee_bps is None:
        return load_cost_model().round_trip_bps
    if legacy_side_fee_bps < 0:
        raise ValueError("fee_bps must be non-negative")
    return legacy_side_fee_bps * 2


def _date(value: str) -> date:
    return date.fromisoformat(value)


def _metrics(value: dict) -> ModelMetrics:
    return ModelMetrics(**value)


def _manifest(path: Path) -> ModelManifest:
    value = json.loads(path.read_text("utf-8"))
    return ModelManifest(
        model_version=value["model_version"],
        feature_version=value["feature_version"],
        model_type=value["model_type"],
        stage=value["stage"],
        created_at=datetime.fromisoformat(value["created_at"]),
        data_cutoff=_date(value["data_cutoff"]),
        artifact_uri=value["artifact_uri"],
        primary_window=value["primary_window"],
        data_source=value["data_source"],
        promotable=bool(value["promotable"]),
        artifact_sha256=value.get("artifact_sha256", ""),
        oos_evaluation_uri=value.get("oos_evaluation_uri", ""),
        oos_evaluation_sha256=value.get("oos_evaluation_sha256", ""),
        holdout_evaluation_uri=value.get("holdout_evaluation_uri", ""),
        holdout_evaluation_sha256=value.get("holdout_evaluation_sha256", ""),
        training_windows=tuple(value.get("training_windows", [])),
        params=value.get("params", {}),
    )


def _evidence(path: Path) -> PromotionEvidence:
    value = json.loads(path.read_text("utf-8"))
    return PromotionEvidence(
        model_version=value["model_version"],
        evaluated_at=datetime.fromisoformat(value["evaluated_at"]),
        stage=value["stage"],
        primary_window=value["primary_window"],
        metrics_source=value["metrics_source"],
        metrics=_metrics(value["metrics"]),
        source_uris=value.get("source_uris", {}),
        source_hashes=value.get("source_hashes", {}),
    )


def cmd_health(args) -> int:
    dependencies = [
        "qlib",
        "lightgbm",
        "optuna",
        "evidently",
        "polars",
        "duckdb",
        "pyarrow",
        "mlflow",
    ]
    status = {name: importlib.util.find_spec(name) is not None for name in dependencies}
    ledger = Path(args.runtime) / "ledger.db"
    result = {
        "python": sys.version.split()[0],
        "dependencies": status,
        "ledger": ledger_counts(ledger) if ledger.exists() else None,
        "registry": load_registry(Path(args.runtime) / "registry"),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if all(status.values()) else 1


def cmd_ledger_init(args) -> int:
    path = Path(args.runtime) / "ledger.db"
    init_ledger(path)
    print(path)
    return 0


def cmd_backfill_outcomes(args) -> int:
    ledger = Path(args.runtime) / "ledger.db"
    init_ledger(ledger)
    predictions = read_predictions(ledger, as_of=_date(args.as_of))
    symbols = sorted({item.symbol for item in predictions})
    if not symbols:
        result = {"as_of": args.as_of, "inserted": 0, "summary": summarize_outcomes(ledger)}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    panel_path = Path(args.panel or Path(args.runtime) / "features" / "panel.parquet")
    panel = pl.read_parquet(panel_path).filter(
        (pl.col("date") <= _date(args.as_of))
        & pl.col("symbol").str.slice(2).is_in(symbols)
    )
    bars = [
        PriceBar(
            date=row["date"],
            symbol=str(row["symbol"])[2:],
            open=float(row["adj_open"]),
            high=float(row["adj_high"]),
            low=float(row["adj_low"]),
            close=float(row["adj_close"]),
        )
        for row in panel.select(
            "date", "symbol", "adj_open", "adj_high", "adj_low", "adj_close"
        ).iter_rows(named=True)
    ]
    inserted = backfill_outcomes(
        ledger,
        bars,
        _date(args.as_of),
        round_trip_fee_bps=_round_trip_fee_bps(args.fee_bps),
    )
    result = {"as_of": args.as_of, "inserted": inserted, "summary": summarize_outcomes(ledger)}
    output = Path(args.output or Path(args.runtime) / "outcomes" / "latest.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", "utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_data_sync(args) -> int:
    runtime = Path(args.runtime)
    try:
        report = sync_tushare(
            runtime / "data",
            _date(args.start),
            _date(args.end),
            env_file=Path(args.env),
            refresh=args.refresh,
            request_interval_seconds=args.request_interval,
            max_workers=args.workers,
        )
    except Exception as error:
        status = runtime / "data" / "meta" / "last-sync-error.json"
        status.parent.mkdir(parents=True, exist_ok=True)
        status.write_text(
            json.dumps(
                {
                    "status": "failed",
                    "start_date": args.start,
                    "end_date": args.end,
                    "error": str(error),
                    "recorded_at": datetime.now().astimezone().isoformat(),
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            "utf-8",
        )
        raise
    error_status = runtime / "data" / "meta" / "last-sync-error.json"
    if error_status.exists():
        error_status.unlink()
    print(
        json.dumps(
            {**report.__dict__, "data_quality_passed": report.data_quality_passed},
            default=str,
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if report.data_quality_passed else 1


def cmd_csi800_sync(args) -> int:
    manifest = sync_csi800_membership(
        Path(args.runtime) / "data",
        _date(args.start),
        _date(args.end),
        env_file=Path(args.env),
        refresh=args.refresh,
        request_interval_seconds=args.request_interval,
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


def cmd_audit_universe_coverage(args) -> int:
    from .universe_coverage import write_coverage_report

    report = write_coverage_report(
        Path(args.runtime) / "data",
        args.output,
        output_text=args.text_output,
        since=args.since,
        expected_member_count=args.expected_member_count,
    )
    print(json.dumps({"passed": report["passed"], "coverage": report["coverage"]}, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


def cmd_sw_industry_sync(args) -> int:
    manifest = sync_sw_industry_membership(
        Path(args.runtime) / "data",
        env_file=Path(args.env),
        request_interval_seconds=args.request_interval,
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


def cmd_bootstrap_qlib(args) -> int:
    result = bootstrap_qlib_dataset(args.runtime, refresh=args.refresh)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_qlib_data_health(args) -> int:
    result = validate_qlib_dataset(args.runtime)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["passed"] else 1


def cmd_qlib_benchmark(args) -> int:
    result = run_alpha158_benchmark(
        args.runtime,
        model_type=args.model_type,
        market=args.market,
        max_folds=args.max_folds,
    )
    print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
    return 0


def cmd_build_features(args) -> int:
    output = Path(args.runtime) / "features" / "panel.parquet"
    result = build_feature_panel(
        Path(args.runtime) / "data",
        output,
        _round_trip_fee_bps(args.fee_bps),
        as_of_date=args.as_of,
    )
    manifest = json.loads(output.with_suffix(".manifest.json").read_text("utf-8"))
    quality = validate_feature_panel(
        pl.read_parquet(output),
        manifest["features"],
        decision_date=args.as_of,
    )
    write_quality_result(quality, Path(args.runtime) / "quality" / "feature-panel.json")
    print(
        json.dumps(
            {**result.__dict__, "quality_passed": quality.passed},
            default=str,
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if quality.passed else 1


def cmd_strategy_race(args) -> int:
    runtime = Path(args.runtime)
    report = run_daily_race(
        panel_path=Path(args.panel or runtime / "features" / "panel.parquet"),
        config_path=Path(
            args.config
            or RESEARCH_ROOT / "config" / "production-race-v1.json"
        ),
        output_path=Path(
            args.output or runtime / "strategy-race" / "daily-report.json"
        ),
        industry_membership_path=(
            Path(args.industry_membership)
            if args.industry_membership
            else None
        ),
        quality_path=Path(args.quality) if args.quality else None,
    )
    print(json.dumps(asdict(report), ensure_ascii=False, indent=2))
    return 0


def cmd_minute_race_plan(args) -> int:
    runtime = Path(args.runtime)
    payload = build_minute_requirement_manifest(
        daily_report_path=Path(
            args.daily_report
            or runtime / "strategy-race" / "daily-report.json"
        ),
        config_path=Path(
            args.config
            or RESEARCH_ROOT / "config" / "production-race-v1.json"
        ),
        output_path=Path(
            args.output
            or runtime / "strategy-race" / "minute-requirements.json"
        ),
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def cmd_minute_race(args) -> int:
    runtime = Path(args.runtime)
    report = run_minute_race(
        daily_report_path=Path(
            args.daily_report
            or runtime / "strategy-race" / "daily-report.json"
        ),
        config_path=Path(
            args.config
            or RESEARCH_ROOT / "config" / "production-race-v1.json"
        ),
        minute_root=Path(args.minute_root or runtime / "minute"),
        requirement_manifest_path=Path(
            args.requirements
            or runtime / "strategy-race" / "minute-requirements.json"
        ),
        output_path=Path(
            args.output
            or runtime / "strategy-race" / "final-report.json"
        ),
    )
    print(json.dumps(asdict(report), ensure_ascii=False, indent=2))
    return 0 if report.production_champion is not None else 1


def cmd_drift(args) -> int:
    panel_path = Path(args.panel or Path(args.runtime) / "features" / "panel.parquet")
    manifest_path = panel_path.with_suffix(".manifest.json")
    if not manifest_path.exists():
        raise FileNotFoundError(f"feature manifest missing: {manifest_path}")
    features = json.loads(manifest_path.read_text("utf-8"))["features"]
    panel = pl.read_parquet(panel_path)
    if all(
        value is not None
        for value in (
            args.reference_start,
            args.reference_end,
            args.current_start,
            args.current_end,
        )
    ):
        reference = panel.filter(
            pl.col("date").is_between(_date(args.reference_start), _date(args.reference_end))
        )
        current = panel.filter(
            pl.col("date").is_between(_date(args.current_start), _date(args.current_end))
        )
    elif any(
        value is not None
        for value in (
            args.reference_start,
            args.reference_end,
            args.current_start,
            args.current_end,
        )
    ):
        raise ValueError("provide all four explicit drift dates or none of them")
    else:
        dates = panel.select("date").unique().sort("date")["date"].to_list()
        required = args.reference_bars + args.current_bars
        if len(dates) < required:
            raise ValueError(f"drift requires {required} dates, panel has {len(dates)}")
        current_dates = dates[-args.current_bars :]
        reference_dates = dates[-required : -args.current_bars]
        reference = panel.filter(pl.col("date").is_in(reference_dates))
        current = panel.filter(pl.col("date").is_in(current_dates))
    if reference.is_empty() or current.is_empty():
        raise ValueError(
            f"drift windows must contain rows: reference={reference.height}, current={current.height}"
        )
    output = Path(args.output or Path(args.runtime) / "drift" / "latest.json")
    result = run_evidently_drift(
        reference,
        current,
        features,
        output,
        args.maximum_drifted_share,
    )
    print(
        json.dumps(
            {key: value for key, value in result.items() if key != "report"},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if result["passed"] else 1


def cmd_train(args) -> int:
    result = train_models(
        Path(args.runtime) / "features" / "panel.parquet",
        Path(args.runtime),
        args.model_type,
        args.optuna_trials,
    )
    print(json.dumps(result.__dict__, ensure_ascii=False, indent=2))
    return 0


def cmd_run_challenger(args) -> int:
    result = run_challenger_experiment(
        args.runtime,
        model_type=args.model_type,
        optuna_trials=args.optuna_trials,
        fee_bps=args.fee_bps,
        max_positions=args.max_positions,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_assess_challenger(args) -> int:
    result = assess_registered_challenger(
        args.runtime,
        args.model_version,
        auto_promote=args.auto_promote,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_build_health(args) -> int:
    result = build_model_health(
        args.state,
        args.baseline_evaluation,
        args.quality,
        args.drift,
        args.as_of,
        args.underperformance_margin,
    )
    write_model_health(result, args.output)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_verify_model(args) -> int:
    manifest = verify_model_artifacts(Path(args.runtime) / "registry", args.model_version)
    print(
        json.dumps(
            {
                "model_version": manifest.model_version,
                "artifact_sha256": manifest.artifact_sha256,
                "status": "verified",
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def cmd_evaluate_oos(args) -> int:
    report = evaluate_oos_predictions(
        Path(args.predictions),
        PortfolioConfig(
            start_cash=args.start_cash,
            max_positions=args.max_positions,
            fee_bps=args.fee_bps,
            min_expected_return=args.min_expected_return,
            switch_buffer=args.switch_buffer,
            rebalance_threshold_pct=args.rebalance_threshold_pct,
        ),
    )
    write_evaluation_report(report, Path(args.output))
    print(
        json.dumps(
            {
                "output": args.output,
                "rank_ic": report.signal.rank_ic,
                "sharpe": report.portfolio.sharpe,
                "max_drawdown_pct": report.portfolio.max_drawdown_pct,
                "turnover_pct": report.portfolio.turnover_pct,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def cmd_build_manifest(args) -> int:
    manifest = build_model_manifest(
        args.training_result,
        args.oos_evaluation,
        args.holdout_evaluation,
    )
    write_model_manifest(manifest, args.output)
    print(json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_export_v1(args) -> int:
    report, metrics = export_v1_baseline(args.backtest, args.output_dir)
    print(
        json.dumps(
            {
                "data_cutoff": report["data_cutoff"],
                "sharpe": metrics.primary_sharpe,
                "max_drawdown_pct": metrics.max_drawdown_pct,
                "turnover_pct": metrics.turnover_pct,
                "warning": report["comparability_warning"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def cmd_build_evidence(args) -> int:
    manifest = _manifest(Path(args.manifest))
    evidence = build_promotion_evidence(
        manifest,
        args.oos_evaluation,
        args.holdout_evaluation,
        args.shadow_evaluation,
        args.champion_evaluation,
        args.champion_metrics,
        args.quality,
        args.drift,
    )
    write_promotion_evidence(evidence, args.output)
    print(json.dumps(asdict(evidence), ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_evaluate_shadow(args) -> int:
    report = evaluate_shadow_account(args.state, Path(args.runtime) / "ledger.db")
    write_shadow_evaluation(report, args.output)
    print(json.dumps(report["portfolio"], ensure_ascii=False, indent=2))
    return 0


def cmd_predict(args) -> int:
    quality_report = json.loads(Path(args.quality).read_text("utf-8"))
    drift_report = json.loads(Path(args.drift).read_text("utf-8"))
    warnings = [str(item) for item in quality_report.get("failures", [])]
    if not drift_report.get("passed", False):
        warnings.append(
            f"feature drift share {drift_report.get('drifted_feature_share', 'unknown')}"
        )
    snapshot = generate_shadow_snapshot(
        Path(args.runtime) / "features" / "panel.parquet",
        Path(args.model_bundle),
        Path(args.universe),
        Path(args.output),
        Path(args.runtime) / "ledger.db",
        args.model_version,
        args.max_positions,
        args.stage,
        quality={
            "data_quality_passed": bool(quality_report.get("passed", False)),
            "drift_passed": bool(drift_report.get("passed", False)),
            "warnings": warnings,
        },
        shadow_state_path=args.account_state,
    )
    print(
        json.dumps(
            {
                "decision_date": snapshot["decision_date"],
                "model_version": snapshot["model_version"],
                "predictions": len(snapshot["predictions"]),
                "stage": snapshot["stage"],
            },
            ensure_ascii=False,
        )
    )
    return 0


def cmd_evaluate(args) -> int:
    candidate = _manifest(Path(args.manifest))
    evidence = _evidence(Path(args.evidence))
    champion = verify_promotion_evidence(candidate, evidence)
    gate = evaluate_promotion(candidate, evidence, champion)
    payload = {"passed": gate.passed, "failures": [failure.__dict__ for failure in gate.failures]}
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if args.register:
        register_candidate(Path(args.runtime) / "registry", candidate)
    if args.promote:
        promote(Path(args.runtime) / "registry", candidate, evidence)
    return 0 if gate.passed else 1


def cmd_rollback(args) -> int:
    restored = rollback(Path(args.runtime) / "registry", args.reason)
    print(json.dumps(restored, ensure_ascii=False, indent=2))
    return 0


def cmd_monitor(args) -> int:
    health_payload = json.loads(Path(args.health).read_text("utf-8"))
    if health_payload.get("as_of") != args.as_of:
        raise ValueError(
            f"health as_of={health_payload.get('as_of')} does not match requested {args.as_of}"
        )
    registry_root = Path(args.runtime) / "registry"
    state = load_registry(registry_root)
    if not state["active"]:
        print(json.dumps({"status": "v1", "reasons": []}, ensure_ascii=False))
        return 0
    health = ModelHealth(
        consecutive_underperform_days=int(health_payload["consecutive_underperform_days"]),
        current_drawdown_pct=float(health_payload["current_drawdown_pct"]),
        data_quality_passed=bool(health_payload["data_quality_passed"]),
        drift_passed=bool(health_payload["drift_passed"]),
    )
    reasons = rollback_reasons(state["active"], health)
    payload: dict = {"status": "healthy", "reasons": list(reasons)}
    if reasons and args.auto_rollback:
        payload["status"] = "rolled_back"
        payload["restored"] = rollback(registry_root, ",".join(reasons))
    elif reasons:
        payload["status"] = "rollback_required"
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if not reasons or args.auto_rollback else 1


# ---------------------------------------------------------------------------
# V1.1 Minute & Rebound commands
# ---------------------------------------------------------------------------


def cmd_minute_probe(args) -> int:
    symbols = [s.strip() for s in args.symbols.split(",")]
    results = probe_minute_data(
        symbols,
        args.date,
        freq=args.freq,
        env_file=Path(args.env),
    )
    output = [r.__dict__ for r in results]
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0 if all(r.passed for r in results) else 1


def _upstream_coverage_status(
    data_root: Path,
    start_date: str,
    end_date: str,
) -> tuple[bool, str]:
    """Require the daily warehouse gate to cover the requested minute range."""
    path = data_root / "meta" / "coverage.json"
    if not path.exists():
        return False, "upstream daily coverage report is missing"
    try:
        payload = json.loads(path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return False, "upstream daily coverage report is invalid"
    if not isinstance(payload, dict):
        return False, "upstream daily coverage report must be an object"
    if not payload.get("passed", False):
        return False, "upstream daily coverage report passed=false"
    start = start_date.replace("-", "")
    end = end_date.replace("-", "")
    coverage_start = str(payload.get("start_date") or "").replace("-", "")
    coverage_end = str(payload.get("end_date") or "").replace("-", "")
    if len(coverage_start) != 8 or len(coverage_end) != 8:
        return False, "upstream daily coverage has invalid date bounds"
    if coverage_start > start:
        return False, "upstream daily coverage starts after requested range"
    if coverage_end < end:
        return False, "upstream daily coverage ends before requested range"
    return True, ""


def cmd_minute_sync(args) -> int:
    runtime = Path(args.runtime)
    minute_root = runtime / "minute"
    data_root = runtime / "data"
    symbols = [s.strip() for s in args.symbols.split(",")] if args.symbols else None
    required_dates: dict[str, set[str]] | None = None
    if args.requirements:
        requirements = json.loads(Path(args.requirements).read_text("utf-8"))
        rows = requirements.get("requirements")
        if not isinstance(rows, list) or not rows:
            raise ValueError("minute requirements are empty or invalid")
        required_dates = {}
        symbols = []
        for row in rows:
            symbol = str(row["symbol"])
            trade_date = str(row["trade_date"]).replace("-", "")
            if not args.start.replace("-", "") <= trade_date <= args.end.replace(
                "-", ""
            ):
                raise ValueError(
                    f"required date {trade_date} is outside sync range"
                )
            ts_code = _symbol_to_ts_code(symbol)
            required_dates.setdefault(ts_code, set()).add(trade_date)
            symbols.append(symbol)
        symbols = list(dict.fromkeys(symbols))

    trading_dates = load_trading_dates(data_root, args.start, args.end)
    daily_ok, daily_failure = _upstream_coverage_status(
        data_root, args.start, args.end
    )
    if not daily_ok or not trading_dates:
        print(
            json.dumps(
                {
                    "status": "blocked",
                    "error": daily_failure
                    if not daily_ok
                    else "authoritative trade calendar is missing",
                },
                ensure_ascii=False,
            )
        )
        return 1
    daily_volume_map = load_daily_volume_map(
        data_root, args.start, args.end
    )
    suspended_map = load_suspended_map(data_root, args.start, args.end)
    if required_dates is not None:
        daily_volume_map = {
            ts_code: dates
            & daily_volume_map.get(ts_code, set())
            for ts_code, dates in required_dates.items()
        }
        suspended_map = {
            ts_code: dates
            & suspended_map.get(ts_code, set())
            for ts_code, dates in required_dates.items()
        }

    report = sync_minute_data(
        minute_root,
        _date(args.start),
        _date(args.end),
        freq=args.freq,
        universe_path=Path(args.universe) if args.universe else None,
        symbols=symbols,
        env_file=Path(args.env),
        refresh=args.refresh,
        request_interval=args.request_interval,
        max_workers=args.max_workers,
        trading_dates=trading_dates,
        expected_dates_by_symbol=daily_volume_map,
        suspended_dates_by_symbol=suspended_map,
    )
    print(json.dumps(report.__dict__, ensure_ascii=False, indent=2, default=str))
    return 0 if report.passed else 1


def cmd_minute_health(args) -> int:
    runtime = Path(args.runtime)
    minute_root = runtime / "minute"
    daily_data_root = runtime / "data"

    trading_dates = load_trading_dates(daily_data_root, args.start, args.end)
    daily_volume_map = load_daily_volume_map(daily_data_root, args.start, args.end)
    suspended_map = load_suspended_map(daily_data_root, args.start, args.end)
    upstream_ok, upstream_failure = _upstream_coverage_status(
        daily_data_root, args.start, args.end
    )

    # CRITICAL: Load expected symbols from universe so that stocks with
    # zero minute data are still checked (fail-closed coverage).
    expected_symbols: list[str] | None = None
    universe_path = (
        Path(args.universe)
        if hasattr(args, "universe") and args.universe
        else None
    )
    universe_failure = ""
    if universe_path is None or not universe_path.is_file():
        universe_failure = "research universe is missing: minute coverage cannot be verified"
    else:
        from .minute_data import _symbol_to_ts_code

        try:
            interval_start = args.start.replace("-", "")
            interval_end = args.end.replace("-", "")
            expected_symbols = [
                _symbol_to_ts_code(symbol)
                for symbol, (active_from, active_until) in (
                    load_universe_membership(universe_path).items()
                )
                if active_from <= interval_end
                and active_until >= interval_start
            ]
            if not expected_symbols:
                raise ValueError(
                    "research universe has no active members "
                    "in the requested interval"
                )
        except (OSError, ValueError, json.JSONDecodeError) as error:
            universe_failure = f"research universe is invalid: {error}"
        else:
            expected_set = set(expected_symbols)
            daily_volume_map = {
                symbol: dates
                for symbol, dates in daily_volume_map.items()
                if symbol in expected_set
            }
            suspended_map = {
                symbol: dates
                for symbol, dates in suspended_map.items()
                if symbol in expected_set
            }

    report = run_minute_quality(
        minute_root,
        args.start,
        args.end,
        freq=args.freq,
        trading_dates=trading_dates,
        daily_volume_map=daily_volume_map,
        suspended_map=suspended_map,
        expected_symbols=expected_symbols,
    )
    if not upstream_ok:
        report.failures.append(upstream_failure)
        report.passed = False
    if universe_failure:
        report.failures.append(universe_failure)
        report.passed = False

    # CRITICAL: Write coverage report to minute_root/meta/coverage.json
    # so that the research pipeline can gate on it.
    meta_dir = minute_root / "meta"
    meta_dir.mkdir(parents=True, exist_ok=True)
    coverage_path = meta_dir / "coverage.json"
    coverage_path.write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2, default=str) + "\n",
        "utf-8",
    )

    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2, default=str))
    return 0 if report.passed else 1


def cmd_rebound_study(args) -> int:
    runtime = Path(args.runtime)
    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = RESEARCH_ROOT / config_path
    summary = run_rebound_study(
        config_path=config_path,
        stage=args.stage,
        runtime_root=runtime / "rebound-v1.1",
        minute_root=runtime / "minute",
        daily_data_root=runtime / "data",
        universe_path=Path(args.universe),
        repo_root=RESEARCH_ROOT.parent,
        bootstrap_seed=args.seed,
    )
    print(json.dumps(summary.to_dict(), ensure_ascii=False, indent=2, default=str))
    return 0 if summary.verdict not in ("blocked", "blocked_by_daily_data") else 1


def cmd_rebound_lock(args) -> int:
    runtime = Path(args.runtime)
    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = RESEARCH_ROOT / config_path
    rebound_root = runtime / "rebound-v1.1"
    coverage_path = runtime / "minute" / "meta" / "coverage.json"

    # CRITICAL: Use latest.json to find actual dev/val report paths.
    latest_path = rebound_root / "latest.json"
    dev_report = ""
    val_report = ""
    if latest_path.exists():
        try:
            latest = json.loads(latest_path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            print(
                json.dumps(
                    {"status": "blocked", "error": f"invalid latest.json: {error}"},
                    ensure_ascii=False,
                )
            )
            return 1
        if not isinstance(latest, dict):
            print(
                json.dumps(
                    {"status": "blocked", "error": "latest.json must be an object"},
                    ensure_ascii=False,
                )
            )
            return 1
        dev_run_id = latest.get("development", "")
        val_run_id = latest.get("validation", "")
        if dev_run_id:
            dev_report = str(rebound_root / dev_run_id / "summary.json")
        if val_run_id:
            val_report = str(rebound_root / val_run_id / "summary.json")

    selected_strategy = args.selected or ""
    if not selected_strategy and val_report:
        try:
            validation_payload = json.loads(Path(val_report).read_text("utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            print(
                json.dumps(
                    {
                        "status": "blocked",
                        "error": f"invalid validation summary: {error}",
                    },
                    ensure_ascii=False,
                )
            )
            return 1
        if isinstance(validation_payload, dict):
            selected_strategy = str(
                validation_payload.get("selected_strategy") or ""
            )

    lock_path = rebound_root / "config-lock.json"
    try:
        lock = create_config_lock(
            config_path=config_path,
            coverage_report_path=coverage_path if coverage_path.exists() else None,
            selected_strategy=selected_strategy,
            dev_report_path=dev_report,
            val_report_path=val_report,
            output_path=lock_path,
            universe_path=Path(args.universe),
        )
    except ValueError as error:
        print(json.dumps({"status": "blocked", "error": str(error)}, ensure_ascii=False))
        return 1
    verified, message = verify_config_lock(config_path, lock_path)
    if not verified:
        print(json.dumps({"status": "blocked", "error": message}, ensure_ascii=False))
        return 1
    print(json.dumps(lock, ensure_ascii=False, indent=2))
    return 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="ashare-research")
    root.add_argument("--runtime", default=str(DEFAULT_RUNTIME))
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("health").set_defaults(func=cmd_health)
    commands.add_parser("ledger-init").set_defaults(func=cmd_ledger_init)
    outcomes = commands.add_parser("backfill-outcomes")
    outcomes.add_argument("--as-of", required=True)
    outcomes.add_argument("--panel")
    outcomes.add_argument(
        "--fee-bps",
        type=float,
        help="legacy symmetric per-side override; defaults to shared cost model",
    )
    outcomes.add_argument("--output")
    outcomes.set_defaults(func=cmd_backfill_outcomes)
    sync = commands.add_parser("data-sync")
    sync.add_argument("--start", required=True)
    sync.add_argument("--end", required=True)
    sync.add_argument("--env", default=str(RESEARCH_ROOT.parent / "pyserver" / ".env"))
    sync.add_argument("--refresh", action="store_true")
    sync.add_argument("--workers", type=int, default=8)
    sync.add_argument("--request-interval", type=float, default=0.12)
    sync.set_defaults(func=cmd_data_sync)
    csi800_sync = commands.add_parser("csi800-sync")
    csi800_sync.add_argument("--start", required=True)
    csi800_sync.add_argument("--end", required=True)
    csi800_sync.add_argument(
        "--env",
        default=str(RESEARCH_ROOT.parent / "pyserver" / ".env"),
    )
    csi800_sync.add_argument("--refresh", action="store_true")
    csi800_sync.add_argument("--request-interval", type=float, default=0.12)
    csi800_sync.set_defaults(func=cmd_csi800_sync)
    sw_sync = commands.add_parser("sw-industry-sync")
    sw_sync.add_argument(
        "--env",
        default=str(RESEARCH_ROOT.parent / "pyserver" / ".env"),
    )
    sw_sync.add_argument("--request-interval", type=float, default=0.12)
    sw_sync.set_defaults(func=cmd_sw_industry_sync)
    audit_universe = commands.add_parser("audit-universe-coverage")
    audit_universe.add_argument("--output", required=True)
    audit_universe.add_argument("--text-output")
    audit_universe.add_argument("--since", default="2018-01-01")
    audit_universe.add_argument("--expected-member-count", type=int, default=800)
    audit_universe.set_defaults(func=cmd_audit_universe_coverage)
    bootstrap = commands.add_parser("bootstrap-qlib")
    bootstrap.add_argument("--refresh", action="store_true")
    bootstrap.set_defaults(func=cmd_bootstrap_qlib)
    commands.add_parser("qlib-data-health").set_defaults(func=cmd_qlib_data_health)
    benchmark = commands.add_parser("qlib-benchmark")
    benchmark.add_argument("--model-type", choices=["linear", "lightgbm"], default="linear")
    benchmark.add_argument("--market", default="csi500")
    benchmark.add_argument("--max-folds", type=int)
    benchmark.set_defaults(func=cmd_qlib_benchmark)
    features = commands.add_parser("build-features")
    features.add_argument(
        "--fee-bps",
        type=float,
        help="legacy symmetric per-side override; defaults to shared cost model",
    )
    features.add_argument("--as-of")
    features.set_defaults(func=cmd_build_features)
    race = commands.add_parser("strategy-race")
    race.add_argument("--panel")
    race.add_argument("--config")
    race.add_argument("--output")
    race.add_argument("--quality")
    race.add_argument("--industry-membership")
    race.set_defaults(func=cmd_strategy_race)
    minute_plan = commands.add_parser("minute-race-plan")
    minute_plan.add_argument("--daily-report")
    minute_plan.add_argument("--config")
    minute_plan.add_argument("--output")
    minute_plan.set_defaults(func=cmd_minute_race_plan)
    minute_race = commands.add_parser("minute-race")
    minute_race.add_argument("--daily-report")
    minute_race.add_argument("--config")
    minute_race.add_argument("--minute-root")
    minute_race.add_argument("--requirements")
    minute_race.add_argument("--output")
    minute_race.set_defaults(func=cmd_minute_race)
    drift = commands.add_parser("drift")
    drift.add_argument("--panel")
    drift.add_argument("--reference-start")
    drift.add_argument("--reference-end")
    drift.add_argument("--current-start")
    drift.add_argument("--current-end")
    drift.add_argument("--reference-bars", type=int, default=120)
    drift.add_argument("--current-bars", type=int, default=20)
    drift.add_argument("--maximum-drifted-share", type=float, default=0.3)
    drift.add_argument("--output")
    drift.set_defaults(func=cmd_drift)
    train = commands.add_parser("train")
    train.add_argument(
        "--model-type", choices=["linear", "lightgbm", "double_ensemble"], default="lightgbm"
    )
    train.add_argument("--optuna-trials", type=int, default=20)
    train.set_defaults(func=cmd_train)
    challenger = commands.add_parser("run-challenger")
    challenger.add_argument(
        "--model-type", choices=["linear", "lightgbm", "double_ensemble"], default="lightgbm"
    )
    challenger.add_argument("--optuna-trials", type=int, default=20)
    challenger.add_argument(
        "--fee-bps",
        type=float,
        help="legacy symmetric per-side override; defaults to shared cost model",
    )
    challenger.add_argument("--max-positions", type=int, default=4)
    challenger.set_defaults(func=cmd_run_challenger)
    assessment = commands.add_parser("assess-challenger")
    assessment.add_argument("--model-version", required=True)
    assessment.add_argument("--auto-promote", action="store_true")
    assessment.set_defaults(func=cmd_assess_challenger)
    health = commands.add_parser("build-health")
    health.add_argument("--state", required=True)
    health.add_argument("--baseline-evaluation", required=True)
    health.add_argument("--quality", required=True)
    health.add_argument("--drift", required=True)
    health.add_argument("--as-of", required=True)
    health.add_argument("--underperformance-margin", type=float, default=0.0025)
    health.add_argument("--output", required=True)
    health.set_defaults(func=cmd_build_health)
    verification = commands.add_parser("verify-model")
    verification.add_argument("--model-version", required=True)
    verification.set_defaults(func=cmd_verify_model)
    evaluation = commands.add_parser("evaluate-oos")
    evaluation.add_argument("--predictions", required=True)
    evaluation.add_argument("--output", required=True)
    evaluation.add_argument("--start-cash", type=float, default=1_000_000)
    evaluation.add_argument("--max-positions", type=int, default=4)
    evaluation.add_argument(
        "--fee-bps",
        type=float,
        help="legacy symmetric per-side override; defaults to shared cost model",
    )
    evaluation.add_argument("--min-expected-return", type=float, default=0.003)
    evaluation.add_argument("--switch-buffer", type=float, default=0.002)
    evaluation.add_argument("--rebalance-threshold-pct", type=float, default=5)
    evaluation.set_defaults(func=cmd_evaluate_oos)
    manifest = commands.add_parser("build-manifest")
    manifest.add_argument("--training-result", required=True)
    manifest.add_argument("--oos-evaluation", required=True)
    manifest.add_argument("--holdout-evaluation", required=True)
    manifest.add_argument("--output", required=True)
    manifest.set_defaults(func=cmd_build_manifest)
    baseline = commands.add_parser("export-v1-baseline")
    baseline.add_argument("--backtest", required=True)
    baseline.add_argument("--output-dir", required=True)
    baseline.set_defaults(func=cmd_export_v1)
    evidence = commands.add_parser("build-promotion-evidence")
    evidence.add_argument("--manifest", required=True)
    evidence.add_argument("--oos-evaluation", required=True)
    evidence.add_argument("--holdout-evaluation", required=True)
    evidence.add_argument("--shadow-evaluation", required=True)
    evidence.add_argument("--champion-evaluation", required=True)
    evidence.add_argument("--champion-metrics", required=True)
    evidence.add_argument("--quality", required=True)
    evidence.add_argument("--drift", required=True)
    evidence.add_argument("--output", required=True)
    evidence.set_defaults(func=cmd_build_evidence)
    shadow = commands.add_parser("evaluate-shadow")
    shadow.add_argument("--state", required=True)
    shadow.add_argument("--output", required=True)
    shadow.set_defaults(func=cmd_evaluate_shadow)
    predict = commands.add_parser("predict")
    predict.add_argument("--model-bundle", required=True)
    predict.add_argument("--model-version", required=True)
    predict.add_argument(
        "--universe", default=str(RESEARCH_ROOT.parent / "web" / "data" / "universe.json")
    )
    predict.add_argument(
        "--output",
        default=str(
            RESEARCH_ROOT.parent / "web" / "data" / "runtime" / "ml" / "shadow-predictions.json"
        ),
    )
    predict.add_argument("--max-positions", type=int, default=4)
    predict.add_argument("--stage", choices=["shadow", "champion"], default="shadow")
    predict.add_argument("--account-state")
    predict.add_argument("--quality", required=True)
    predict.add_argument("--drift", required=True)
    predict.set_defaults(func=cmd_predict)
    evaluate = commands.add_parser("evaluate-promotion")
    evaluate.add_argument("--manifest", required=True)
    evaluate.add_argument("--evidence", required=True)
    evaluate.add_argument("--register", action="store_true")
    evaluate.add_argument("--promote", action="store_true")
    evaluate.set_defaults(func=cmd_evaluate)
    rollback_parser = commands.add_parser("rollback")
    rollback_parser.add_argument("--reason", required=True)
    rollback_parser.set_defaults(func=cmd_rollback)
    monitor = commands.add_parser("monitor")
    monitor.add_argument("--health", required=True)
    monitor.add_argument("--as-of", required=True)
    monitor.add_argument("--auto-rollback", action="store_true")
    monitor.set_defaults(func=cmd_monitor)

    # V1.1 Minute & Rebound commands
    mprobe = commands.add_parser("minute-probe")
    mprobe.add_argument("--symbols", required=True, help="Comma-separated symbols")
    mprobe.add_argument("--date", required=True, help="Probe date YYYY-MM-DD")
    mprobe.add_argument("--freq", default="5min")
    mprobe.add_argument("--env", default=str(RESEARCH_ROOT.parent / "pyserver" / ".env"))
    mprobe.set_defaults(func=cmd_minute_probe)

    msync = commands.add_parser("minute-sync")
    msync.add_argument("--start", required=True)
    msync.add_argument("--end", required=True)
    msync.add_argument("--freq", default="5min")
    msync.add_argument(
        "--universe",
        default=str(RESEARCH_ROOT.parent / "web" / "data" / "universe.json"),
    )
    msync.add_argument("--symbols", default=None)
    msync.add_argument(
        "--requirements",
        help="sparse minute-race requirement manifest",
    )
    msync.add_argument("--env", default=str(RESEARCH_ROOT.parent / "pyserver" / ".env"))
    msync.add_argument("--refresh", action="store_true")
    msync.add_argument("--request-interval", type=float, default=0.15)
    msync.add_argument("--max-workers", type=int, choices=[1, 2], default=1)
    msync.set_defaults(func=cmd_minute_sync)

    mhealth = commands.add_parser("minute-health")
    mhealth.add_argument("--start", required=True)
    mhealth.add_argument("--end", required=True)
    mhealth.add_argument("--freq", default="5min")
    mhealth.add_argument(
        "--universe",
        default=str(RESEARCH_ROOT.parent / "web" / "data" / "universe.json"),
    )
    mhealth.set_defaults(func=cmd_minute_health)

    rstudy = commands.add_parser("rebound-study")
    rstudy.add_argument("--stage", required=True, choices=["development", "validation", "frozen"])
    rstudy.add_argument("--config", required=True)
    rstudy.add_argument(
        "--universe", default=str(RESEARCH_ROOT.parent / "web" / "data" / "universe.json")
    )
    rstudy.add_argument("--seed", type=int, default=None)
    rstudy.set_defaults(func=cmd_rebound_study)

    rlock = commands.add_parser("rebound-lock")
    rlock.add_argument("--config", required=True)
    rlock.add_argument("--selected", default="")
    rlock.add_argument(
        "--universe",
        default=str(
            RESEARCH_ROOT.parent / "web" / "data" / "universe.json"
        ),
    )
    rlock.set_defaults(func=cmd_rebound_lock)

    return root


def main() -> None:
    args = parser().parse_args()
    raise SystemExit(args.func(args))


if __name__ == "__main__":
    main()
