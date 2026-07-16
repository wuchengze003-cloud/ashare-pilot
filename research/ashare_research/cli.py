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
from .data_sync import sync_tushare
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
from .monitoring import ModelHealth, rollback_reasons
from .portfolio import PortfolioConfig
from .promotion import evaluate_promotion
from .qlib_benchmark import run_alpha158_benchmark
from .qlib_bootstrap import bootstrap_qlib_dataset, validate_qlib_dataset
from .quality import run_evidently_drift, validate_feature_panel, write_quality_result
from .registry import (
    load_registry,
    promote,
    register_candidate,
    rollback,
    verify_model_artifacts,
    verify_promotion_evidence,
)
from .shadow_evaluation import evaluate_shadow_account, write_shadow_evaluation
from .training import train_models

RESEARCH_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RUNTIME = RESEARCH_ROOT / "runtime"


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
        round_trip_fee_bps=args.fee_bps * 2,
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
        args.fee_bps * 2,
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


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="ashare-research")
    root.add_argument("--runtime", default=str(DEFAULT_RUNTIME))
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("health").set_defaults(func=cmd_health)
    commands.add_parser("ledger-init").set_defaults(func=cmd_ledger_init)
    outcomes = commands.add_parser("backfill-outcomes")
    outcomes.add_argument("--as-of", required=True)
    outcomes.add_argument("--panel")
    outcomes.add_argument("--fee-bps", type=float, default=10)
    outcomes.add_argument("--output")
    outcomes.set_defaults(func=cmd_backfill_outcomes)
    sync = commands.add_parser("data-sync")
    sync.add_argument("--start", required=True)
    sync.add_argument("--end", required=True)
    sync.add_argument("--env", default=str(RESEARCH_ROOT.parent / "pyserver" / ".env"))
    sync.add_argument("--refresh", action="store_true")
    sync.set_defaults(func=cmd_data_sync)
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
    features.add_argument("--fee-bps", type=float, default=10)
    features.add_argument("--as-of")
    features.set_defaults(func=cmd_build_features)
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
    challenger.add_argument("--fee-bps", type=float, default=10)
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
    evaluation.add_argument("--fee-bps", type=float, default=10)
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
    return root


def main() -> None:
    args = parser().parse_args()
    raise SystemExit(args.func(args))


if __name__ == "__main__":
    main()
