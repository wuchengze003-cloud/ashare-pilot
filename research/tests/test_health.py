import json

from ashare_research.health import build_model_health


def write(path, value):
    path.write_text(json.dumps(value), "utf-8")


def test_health_counts_only_trailing_material_underperformance(tmp_path):
    dates = [f"2026-07-{day:02d}" for day in range(1, 13)]
    baseline_curve = []
    active_curve = []
    baseline_equity = 100.0
    active_equity = 100.0
    for index, value in enumerate(dates):
        baseline_equity *= 1.001
        active_equity *= 1.001 if index < 2 else 0.997
        baseline_curve.append({"date": value, "equity": baseline_equity})
        active_curve.append({"date": value, "equity": active_equity})
    state = tmp_path / "state.json"
    baseline = tmp_path / "baseline.json"
    quality = tmp_path / "quality.json"
    drift = tmp_path / "drift.json"
    write(
        state,
        {
            "model_version": "linear-001",
            "start_cash": 100,
            "equity_curve": active_curve,
        },
    )
    write(baseline, {"portfolio": {"equity_curve": baseline_curve}})
    write(quality, {"passed": True})
    write(drift, {"passed": True})

    result = build_model_health(
        state,
        baseline,
        quality,
        drift,
        "2026-07-12",
        underperformance_margin=0.0025,
    )

    assert result["consecutive_underperform_days"] == 10
    assert result["current_drawdown_pct"] < 0
    assert result["data_quality_passed"]
    assert result["drift_passed"]
