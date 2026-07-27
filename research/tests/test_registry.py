import json
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from ashare_research.candidate import build_promotion_evidence
from ashare_research.contracts import ModelManifest, ModelMetrics
from ashare_research.evaluation import file_sha256
from ashare_research.registry import (
    RegistryError,
    load_registry,
    promote,
    register_candidate,
    rollback,
    verify_model_artifacts,
)


def curve(daily_return: float, days: int = 65):
    equity = 1_000_000.0
    result = []
    for index in range(days):
        equity *= 1 + daily_return
        result.append(
            {
                "date": (date(2026, 1, 1) + timedelta(days=index)).isoformat(),
                "equity": equity,
            }
        )
    return result


def report(sharpe: float, daily_return: float, *, oos_folds: int = 6):
    return {
        "universe_scope": "production-ai-point-in-time",
        "portfolio": {
            "sharpe": sharpe,
            "max_drawdown_pct": -9,
            "average_hold_bars": 6,
            "turnover_pct": 100,
            "closed_trades": 25,
            "equity_curve": curve(daily_return),
        },
        "double_cost_portfolio": {"total_return_pct": 5},
        "oos_folds": oos_folds,
    }


def write_json(path: Path, value: dict):
    path.write_text(json.dumps(value), "utf-8")


def manifest(root, version: str, primary_sharpe: float = 3.2):
    model_dir = root.parent / "models" / version
    model_dir.mkdir(parents=True, exist_ok=True)
    artifact = model_dir / "model-bundle.pkl"
    artifact.write_bytes(version.encode())
    oos = root / f"{version}-oos.json"
    holdout = root / f"{version}-holdout.json"
    write_json(oos, report(2.0, 0.008))
    write_json(holdout, report(primary_sharpe, 0.01))
    item = ModelManifest(
        model_version=version,
        feature_version="alpha-core-v1",
        model_type="lightgbm",
        stage="shadow",
        created_at=datetime(2026, 7, 1, tzinfo=UTC),
        data_cutoff=date(2026, 6, 30),
        artifact_uri=f"models:/{version}",
        primary_window="post_cny_2026",
        data_source="tushare-pro-point-in-time",
        promotable=True,
        artifact_sha256=file_sha256(artifact),
        oos_evaluation_uri=str(oos),
        oos_evaluation_sha256=file_sha256(oos),
        holdout_evaluation_uri=str(holdout),
        holdout_evaluation_sha256=file_sha256(holdout),
    )
    shadow = root / f"{version}-shadow.json"
    champion_evaluation = root / f"{version}-champion.json"
    champion_metrics = root / f"{version}-champion-metrics.json"
    quality = root / f"{version}-quality.json"
    drift = root / f"{version}-drift.json"
    write_json(shadow, report(2.5, 0.009))
    write_json(champion_evaluation, report(1.7, 0.0, oos_folds=0))
    write_json(champion_metrics, asdict(champion()))
    write_json(quality, {"passed": True})
    write_json(drift, {"passed": True})
    promotion = build_promotion_evidence(
        item,
        oos,
        holdout,
        shadow,
        champion_evaluation,
        champion_metrics,
        quality,
        drift,
    )
    return item, promotion


def metrics(**overrides):
    values = {
        "primary_sharpe": 3.2,
        "oos_sharpe": 2.0,
        "max_drawdown_pct": -9,
        "double_cost_return_pct": 5,
        "average_hold_bars": 6,
        "turnover_pct": 100,
        "bootstrap_win_probability": 0.85,
        "shadow_trading_days": 65,
        "closed_trades": 25,
        "oos_folds": 6,
        "data_quality_passed": True,
        "drift_passed": True,
    }
    values.update(overrides)
    return ModelMetrics(**values)


def champion():
    return metrics(oos_sharpe=1.7, max_drawdown_pct=-12, turnover_pct=90)


def test_failed_gate_cannot_promote(tmp_path):
    item, promotion = manifest(tmp_path, "lgbm-001", primary_sharpe=2.9)
    register_candidate(tmp_path, item)
    with pytest.raises(RegistryError, match="gate failed"):
        promote(tmp_path, item, promotion)
    assert load_registry(tmp_path)["active"] is None


def test_promote_and_rollback_preserve_previous_model(tmp_path):
    first, first_evidence = manifest(tmp_path, "lgbm-001")
    second, second_evidence = manifest(tmp_path, "lgbm-002")
    promote(tmp_path, first, first_evidence)
    promote(tmp_path, second, second_evidence)
    assert load_registry(tmp_path)["active"]["model_version"] == "lgbm-002"
    restored = rollback(tmp_path, "performance decay")
    assert restored["model_version"] == "lgbm-001"
    assert restored["rolled_back_from"] == "lgbm-002"


def test_first_ml_model_rolls_back_to_v1(tmp_path):
    first, promotion = manifest(tmp_path, "lgbm-001")
    promote(tmp_path, first, promotion)
    restored = rollback(tmp_path, "drift")
    assert restored["model_version"] == "V1"
    state = load_registry(tmp_path)
    assert state["active"] is None
    assert state["retired"][-1]["model_version"] == "lgbm-001"


def test_candidate_registration_rejects_tampered_evaluation(tmp_path):
    item, _ = manifest(tmp_path, "lgbm-001")
    Path(item.oos_evaluation_uri).write_text('{"tampered":true}', "utf-8")
    with pytest.raises(RegistryError, match="hash mismatch"):
        register_candidate(tmp_path, item)


def test_inference_verification_rejects_tampered_model_artifact(tmp_path):
    item, _ = manifest(tmp_path, "lgbm-001")
    register_candidate(tmp_path, item)
    (tmp_path.parent / "models" / "lgbm-001" / "model-bundle.pkl").write_bytes(b"changed")

    with pytest.raises(RegistryError, match="artifact hash mismatch"):
        verify_model_artifacts(tmp_path, "lgbm-001")


def test_promotion_rejects_tampered_system_evidence(tmp_path):
    item, promotion = manifest(tmp_path, "lgbm-001")
    Path(promotion.source_uris["champion_metrics"]).write_text("{}", "utf-8")
    with pytest.raises(RegistryError, match="source hash mismatch"):
        promote(tmp_path, item, promotion)


def test_candidate_registry_preserves_registration_order(tmp_path):
    linear, _ = manifest(tmp_path, "linear-999")
    lightgbm, _ = manifest(tmp_path, "lightgbm-001")
    register_candidate(tmp_path, linear)
    register_candidate(tmp_path, lightgbm)

    assert load_registry(tmp_path)["candidates"] == ["linear-999", "lightgbm-001"]
