"""Immutable model manifests and system-generated promotion evidence."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, date, datetime
from pathlib import Path

import numpy as np

from .contracts import ModelManifest, ModelMetrics, PromotionEvidence
from .evaluation import file_sha256


def _read_json(path: Path | str) -> dict:
    return json.loads(Path(path).read_text("utf-8"))


def _write_json(path: Path | str, payload: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n", "utf-8")


def build_model_manifest(
    training_result_path: Path | str,
    oos_evaluation_path: Path | str,
    holdout_evaluation_path: Path | str,
) -> ModelManifest:
    training_result_path = Path(training_result_path)
    oos_evaluation_path = Path(oos_evaluation_path)
    holdout_evaluation_path = Path(holdout_evaluation_path)
    training = _read_json(training_result_path)
    model_version = str(training["model_version"])
    artifact_path = Path(training["artifact_path"])
    if not artifact_path.exists():
        raise FileNotFoundError(f"model artifact missing: {artifact_path}")
    if _read_json(oos_evaluation_path)["source_sha256"] != file_sha256(
        training["oos_prediction_path"]
    ):
        raise ValueError("OOS evaluation does not match training predictions")
    holdout_evaluation = _read_json(holdout_evaluation_path)
    if holdout_evaluation["source_sha256"] != file_sha256(
        training["holdout_prediction_path"]
    ):
        raise ValueError("holdout evaluation does not match frozen predictions")
    if holdout_evaluation.get("universe_scope") != "production-ai-point-in-time":
        raise ValueError("holdout evaluation must use point-in-time production AI universe")
    return ModelManifest(
        model_version=model_version,
        feature_version=str(training["feature_version"]),
        model_type=training["model_type"],
        stage="shadow",
        created_at=datetime.now(UTC),
        data_cutoff=date.fromisoformat(training["data_cutoff"]),
        artifact_uri=f"models:/{model_version}",
        primary_window="post_cny_2026",
        data_source=training["data_source"],
        promotable=bool(training["promotable"]),
        artifact_sha256=file_sha256(artifact_path),
        oos_evaluation_uri=str(oos_evaluation_path.resolve()),
        oos_evaluation_sha256=file_sha256(oos_evaluation_path),
        holdout_evaluation_uri=str(holdout_evaluation_path.resolve()),
        holdout_evaluation_sha256=file_sha256(holdout_evaluation_path),
        training_windows=tuple(training["training_windows"]),
        params=dict(training.get("parameters", {})),
    )


def _daily_returns(report: dict) -> dict[str, float]:
    points = report["portfolio"]["equity_curve"]
    result: dict[str, float] = {}
    previous: float | None = None
    for point in points:
        equity = float(point["equity"])
        if previous and previous > 0:
            result[str(point["date"])] = equity / previous - 1
        previous = equity
    return result


def bootstrap_superiority_probability(
    candidate_report: dict,
    champion_report: dict,
    samples: int = 2_000,
    block_size: int = 5,
    seed: int = 20260716,
) -> float:
    candidate = _daily_returns(candidate_report)
    champion = _daily_returns(champion_report)
    dates = sorted(set(candidate) & set(champion))
    if len(dates) < max(20, block_size * 2):
        return 0.0
    excess = np.asarray([candidate[value] - champion[value] for value in dates])
    starts = np.arange(0, len(excess) - block_size + 1)
    rng = np.random.default_rng(seed)
    wins = 0
    blocks_needed = int(np.ceil(len(excess) / block_size))
    for _ in range(samples):
        selected = rng.choice(starts, size=blocks_needed, replace=True)
        sampled = np.concatenate([excess[start : start + block_size] for start in selected])[
            : len(excess)
        ]
        wins += int(float(sampled.mean()) > 0)
    return wins / samples


def build_promotion_evidence(
    manifest: ModelManifest,
    oos_evaluation_path: Path | str,
    holdout_evaluation_path: Path | str,
    shadow_evaluation_path: Path | str,
    champion_evaluation_path: Path | str,
    champion_metrics_path: Path | str,
    quality_path: Path | str,
    drift_path: Path | str,
) -> PromotionEvidence:
    paths = {
        "oos": Path(oos_evaluation_path),
        "holdout": Path(holdout_evaluation_path),
        "shadow": Path(shadow_evaluation_path),
        "champion": Path(champion_evaluation_path),
        "champion_metrics": Path(champion_metrics_path),
        "quality": Path(quality_path),
        "drift": Path(drift_path),
    }
    oos = _read_json(paths["oos"])
    holdout = _read_json(paths["holdout"])
    shadow = _read_json(paths["shadow"])
    champion = _read_json(paths["champion"])
    quality = _read_json(paths["quality"])
    drift = _read_json(paths["drift"])
    if holdout.get("universe_scope") != "production-ai-point-in-time":
        raise ValueError("promotion holdout is not scoped to production AI universe")
    if file_sha256(paths["oos"]) != manifest.oos_evaluation_sha256:
        raise ValueError("OOS evaluation hash does not match immutable manifest")
    if file_sha256(paths["holdout"]) != manifest.holdout_evaluation_sha256:
        raise ValueError("holdout evaluation hash does not match immutable manifest")
    primary = holdout["portfolio"]
    shadow_portfolio = shadow["portfolio"]
    metrics = ModelMetrics(
        primary_sharpe=float(primary["sharpe"]),
        oos_sharpe=float(oos["portfolio"]["sharpe"]),
        max_drawdown_pct=float(primary["max_drawdown_pct"]),
        double_cost_return_pct=float(holdout["double_cost_portfolio"]["total_return_pct"]),
        average_hold_bars=float(primary["average_hold_bars"]),
        turnover_pct=float(primary["turnover_pct"]),
        bootstrap_win_probability=bootstrap_superiority_probability(holdout, champion),
        shadow_trading_days=len(shadow_portfolio["equity_curve"]),
        closed_trades=int(shadow_portfolio["closed_trades"]),
        oos_folds=int(oos["oos_folds"]),
        data_quality_passed=bool(quality["passed"]),
        drift_passed=bool(drift["passed"]),
    )
    return PromotionEvidence(
        model_version=manifest.model_version,
        evaluated_at=datetime.now(UTC),
        stage="shadow",
        primary_window="post_cny_2026",
        metrics_source="system-evaluation-v2",
        metrics=metrics,
        source_uris={name: str(path.resolve()) for name, path in paths.items()},
        source_hashes={name: file_sha256(path) for name, path in paths.items()},
    )


def write_model_manifest(manifest: ModelManifest, path: Path | str) -> None:
    _write_json(path, asdict(manifest))


def write_promotion_evidence(evidence: PromotionEvidence, path: Path | str) -> None:
    _write_json(path, asdict(evidence))
