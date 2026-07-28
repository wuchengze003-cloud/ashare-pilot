"""Deterministic production-data challenger build pipeline."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import polars as pl

from .candidate import build_model_manifest, write_model_manifest
from .cost_config import load_cost_model
from .evaluation import evaluate_oos_predictions, write_evaluation_report
from .features import build_feature_panel
from .portfolio import PortfolioConfig
from .quality import (
    run_latest_evidently_drift,
    validate_feature_panel,
    write_quality_result,
)
from .registry import register_candidate
from .training import train_models


def run_challenger_experiment(
    runtime_root: Path | str,
    model_type: str = "lightgbm",
    optuna_trials: int = 20,
    fee_bps: float | None = None,
    max_positions: int = 4,
    universe_path: Path | str | None = None,
) -> dict:
    cost = load_cost_model()
    if fee_bps is None:
        fee_bps = cost.avg_side_bps  # 8.0 bps per side from unified model
    runtime = Path(runtime_root)
    panel_path = runtime / "features" / "panel.parquet"
    feature_result = build_feature_panel(runtime / "data", panel_path, cost.round_trip_bps)
    panel = pl.read_parquet(panel_path)
    features = list(feature_result.feature_names)

    quality_path = runtime / "quality" / "feature-panel.json"
    quality = validate_feature_panel(panel, features)
    write_quality_result(quality, quality_path)
    if not quality.passed:
        raise ValueError(f"feature quality failed: {quality.failures}")

    drift_path = runtime / "drift" / "latest.json"
    drift = run_latest_evidently_drift(panel, features, drift_path)
    if not drift["passed"]:
        raise ValueError(
            f"feature drift failed: share={drift['drifted_feature_share']}, "
            f"limit={drift['maximum_drifted_share']}"
        )

    training = train_models(panel_path, runtime, model_type, optuna_trials)
    model_dir = Path(training.artifact_path).parent
    config = PortfolioConfig(max_positions=max_positions, fee_bps=fee_bps)
    oos_report = evaluate_oos_predictions(training.oos_prediction_path, config)
    universe_path = (
        universe_path
        or Path(__file__).resolve().parents[2] / "web" / "data" / "universe.json"
    )
    holdout_report = evaluate_oos_predictions(
        training.holdout_prediction_path,
        config,
        universe_path=universe_path,
    )
    oos_path = model_dir / "oos-evaluation.json"
    holdout_path = model_dir / "holdout-evaluation.json"
    write_evaluation_report(oos_report, oos_path)
    write_evaluation_report(holdout_report, holdout_path)

    training_path = model_dir / "training-result.json"
    manifest = build_model_manifest(training_path, oos_path, holdout_path)
    manifest_path = model_dir / "manifest.json"
    write_model_manifest(manifest, manifest_path)
    registry_manifest = register_candidate(runtime / "registry", manifest)

    summary = {
        "status": "registered-for-shadow",
        "model_version": manifest.model_version,
        "model_type": manifest.model_type,
        "data_source": manifest.data_source,
        "promotable": manifest.promotable,
        "data_cutoff": manifest.data_cutoff.isoformat(),
        "oos_folds": oos_report.oos_folds,
        "oos_rank_ic": oos_report.signal.rank_ic,
        "oos_sharpe": oos_report.portfolio.sharpe,
        "holdout_sharpe": holdout_report.portfolio.sharpe,
        "holdout_max_drawdown_pct": holdout_report.portfolio.max_drawdown_pct,
        "quality_path": str(quality_path.resolve()),
        "drift_path": str(drift_path.resolve()),
        "manifest_path": str(manifest_path.resolve()),
        "feature_importance_path": str((model_dir / "feature-importance.json").resolve()),
        "registry_manifest_path": str(registry_manifest.resolve()),
    }
    (model_dir / "challenger-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        "utf-8",
    )
    return {**summary, "training": asdict(training)}
