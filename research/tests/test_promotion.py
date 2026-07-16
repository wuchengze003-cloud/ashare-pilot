from datetime import UTC, date, datetime

from ashare_research.contracts import ModelManifest, ModelMetrics, PromotionEvidence
from ashare_research.promotion import evaluate_promotion


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


def manifest(**overrides):
    values = {
        "model_version": "lgbm-001",
        "feature_version": "alpha-core-v1",
        "model_type": "lightgbm",
        "stage": "shadow",
        "created_at": datetime(2026, 7, 1, tzinfo=UTC),
        "data_cutoff": date(2026, 6, 30),
        "artifact_uri": "models:/lgbm-001",
        "primary_window": "post_cny_2026",
        "data_source": "tushare-pro-point-in-time",
        "promotable": True,
    }
    values.update(overrides)
    return ModelManifest(**values)


def champion():
    return metrics(primary_sharpe=2.3, oos_sharpe=1.7, max_drawdown_pct=-12, turnover_pct=90)


def evidence(**overrides):
    values = {
        "model_version": "lgbm-001",
        "evaluated_at": datetime(2026, 7, 1, tzinfo=UTC),
        "stage": "shadow",
        "primary_window": "post_cny_2026",
        "metrics_source": "system-evaluation-v2",
        "metrics": metrics(),
    }
    values.update(overrides)
    return PromotionEvidence(**values)


def test_promotion_passes_all_hard_gates():
    result = evaluate_promotion(manifest(), evidence(), champion())
    assert result.passed
    assert result.failures == ()


def test_oos_improvement_is_absolute_quarter_point():
    result = evaluate_promotion(manifest(), evidence(metrics=metrics(oos_sharpe=1.94)), champion())
    assert not result.passed
    assert "OOS_SHARPE_IMPROVEMENT" in {failure.code for failure in result.failures}


def test_promotion_rejects_excess_drawdown_and_turnover():
    result = evaluate_promotion(
        manifest(),
        evidence(metrics=metrics(max_drawdown_pct=-10, turnover_pct=109)),
        champion(),
    )
    assert {failure.code for failure in result.failures} >= {"MAX_DRAWDOWN", "TURNOVER"}


def test_promotion_rejects_short_shadow_or_failed_drift():
    result = evaluate_promotion(
        manifest(),
        evidence(metrics=metrics(shadow_trading_days=59, drift_passed=False)),
        champion(),
    )
    assert {failure.code for failure in result.failures} >= {"SHADOW_DAYS", "DRIFT"}


def test_promotion_rejects_evidence_for_another_model():
    result = evaluate_promotion(manifest(), evidence(model_version="lgbm-other"), champion())
    assert "MODEL_VERSION_MATCH" in {failure.code for failure in result.failures}


def test_public_cold_start_model_cannot_promote():
    result = evaluate_promotion(
        manifest(data_source="community-qlib-cold-start", promotable=False),
        evidence(),
        champion(),
    )
    assert {failure.code for failure in result.failures} >= {
        "PROMOTABLE_MODEL",
        "PRODUCTION_DATA_SOURCE",
    }
