from ashare_research.monitoring import ModelHealth, rollback_reasons


def test_health_guard_triggers_every_required_rollback_reason():
    active = {"rollback_drawdown_limit_pct": 10.0}
    health = ModelHealth(
        consecutive_underperform_days=10,
        current_drawdown_pct=-10.1,
        data_quality_passed=False,
        drift_passed=False,
    )
    assert set(rollback_reasons(active, health)) == {
        "UNDERPERFORMED_10_DAYS",
        "DRAWDOWN_GUARD_BREACHED",
        "DATA_QUALITY_FAILED",
        "DRIFT_FAILED",
    }


def test_health_guard_accepts_boundary_values():
    active = {"rollback_drawdown_limit_pct": 10.0}
    health = ModelHealth(9, -10.0, True, True)
    assert rollback_reasons(active, health) == ()
