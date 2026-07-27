import json
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta

from ashare_research.assessment import assess_registered_challenger
from ashare_research.contracts import ModelManifest, ModelMetrics
from ashare_research.evaluation import file_sha256
from ashare_research.registry import register_candidate


def curve(daily_return: float, days: int = 65):
    equity = 1_000_000.0
    result = []
    for index in range(days):
        equity *= 1 + daily_return
        result.append(
            {
                "date": (date(2026, 2, 24) + timedelta(days=index)).isoformat(),
                "equity": equity,
            }
        )
    return result


def report(sharpe: float, daily_return: float, oos_folds: int = 6):
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


def test_assessment_records_missing_shadow_evidence_without_promoting(tmp_path):
    version = "linear-001"
    model_dir = tmp_path / "models" / version
    model_dir.mkdir(parents=True)
    artifact = model_dir / "model-bundle.pkl"
    artifact.write_bytes(b"model")
    oos = tmp_path / "oos.json"
    holdout = tmp_path / "holdout.json"
    oos.write_text("{}", "utf-8")
    holdout.write_text("{}", "utf-8")
    manifest = ModelManifest(
        model_version=version,
        feature_version="ashare-core-v2",
        model_type="linear",
        stage="shadow",
        created_at=datetime(2026, 7, 16, tzinfo=UTC),
        data_cutoff=date(2026, 7, 16),
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
    register_candidate(tmp_path / "registry", manifest)

    result = assess_registered_challenger(tmp_path, version, auto_promote=True)

    assert result["status"] == "shadow-not-ready"
    assert not result["passed"]
    assert {failure["actual"] for failure in result["failures"]} == {
        "shadow",
        "quality",
        "drift",
    }
    registry = json.loads((tmp_path / "registry" / "active_model.json").read_text("utf-8"))
    assert registry["active"] is None


def test_assessment_auto_promotes_only_after_all_system_gates_pass(tmp_path):
    version = "linear-002"
    model_dir = tmp_path / "models" / version
    model_dir.mkdir(parents=True)
    artifact = model_dir / "model-bundle.pkl"
    artifact.write_bytes(b"model")
    oos = model_dir / "oos.json"
    holdout = model_dir / "holdout.json"
    oos.write_text(json.dumps(report(2.0, 0.008)), "utf-8")
    holdout.write_text(json.dumps(report(3.2, 0.01)), "utf-8")
    manifest = ModelManifest(
        model_version=version,
        feature_version="ashare-core-v2",
        model_type="linear",
        stage="shadow",
        created_at=datetime(2026, 7, 16, tzinfo=UTC),
        data_cutoff=date(2026, 7, 16),
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
    register_candidate(tmp_path / "registry", manifest)

    evaluation_dir = tmp_path / "evaluations" / version
    evaluation_dir.mkdir(parents=True)
    (evaluation_dir / "shadow.json").write_text(json.dumps(report(2.5, 0.009)), "utf-8")
    baseline_dir = tmp_path / "baselines" / "v1"
    baseline_dir.mkdir(parents=True)
    (baseline_dir / "evaluation.json").write_text(json.dumps(report(1.7, 0.0)), "utf-8")
    champion = ModelMetrics(
        primary_sharpe=1.8,
        oos_sharpe=1.7,
        max_drawdown_pct=-12,
        double_cost_return_pct=1,
        average_hold_bars=6,
        turnover_pct=90,
        bootstrap_win_probability=0,
        shadow_trading_days=65,
        closed_trades=25,
        oos_folds=6,
        data_quality_passed=True,
        drift_passed=True,
    )
    (baseline_dir / "metrics.json").write_text(json.dumps(asdict(champion)), "utf-8")
    quality_dir = tmp_path / "quality"
    drift_dir = tmp_path / "drift"
    quality_dir.mkdir()
    drift_dir.mkdir()
    (quality_dir / "feature-panel.json").write_text('{"passed":true}', "utf-8")
    (drift_dir / "latest.json").write_text('{"passed":true}', "utf-8")

    result = assess_registered_challenger(tmp_path, version, auto_promote=True)

    assert result["status"] == "promoted"
    assert result["passed"]
    registry = json.loads((tmp_path / "registry" / "active_model.json").read_text("utf-8"))
    assert registry["active"]["model_version"] == version
    assert registry["active"]["champion_metrics_sha256"]
