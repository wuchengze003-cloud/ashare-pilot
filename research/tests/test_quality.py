import json

import polars as pl

from ashare_research.quality import (
    run_evidently_drift,
    run_latest_evidently_drift,
    validate_feature_panel,
)


def _quality_panel(feature: list[float | None]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "date": list(range(len(feature))),
            "symbol": [f"S{index}" for index in range(len(feature))],
            "log_pe_ttm": feature,
        }
    )


def test_structural_pe_missing_rate_uses_explicit_30_percent_limit():
    panel = _quality_panel([None] * 29 + [1.0] * 71)

    result = validate_feature_panel(panel, ["log_pe_ttm"])

    assert result.passed
    assert result.missing_feature_rates["log_pe_ttm"] == 0.29
    assert result.missing_feature_limits["log_pe_ttm"] == 0.30


def test_structural_pe_missing_rate_above_30_percent_fails():
    panel = _quality_panel([None] * 31 + [1.0] * 69)

    result = validate_feature_panel(panel, ["log_pe_ttm"])

    assert not result.passed
    assert "log_pe_ttm" in result.failures[0]


def test_other_features_keep_25_percent_limit_and_nan_counts_as_missing():
    panel = _quality_panel([1.0] * 100).with_columns(
        pl.Series("momentum", [None] * 25 + [float("nan")] + [1.0] * 74)
    )

    result = validate_feature_panel(panel, ["momentum"])

    assert not result.passed
    assert result.missing_feature_rates["momentum"] == 0.26
    assert result.missing_feature_limits["momentum"] == 0.25


def test_moneyflow_quality_accepts_non_degenerate_values():
    panel = pl.DataFrame(
        {
            "date": [1, 2, 3],
            "symbol": ["A", "B", "C"],
            "net_moneyflow_ratio": [0.1, None, -0.2],
            "large_order_ratio": [0.2, 0.1, -0.1],
        }
    )

    result = validate_feature_panel(
        panel,
        ["net_moneyflow_ratio", "large_order_ratio"],
        max_missing_rate=0.5,
    )

    assert result.passed
    assert result.moneyflow_status == "available"


def test_moneyflow_quality_rejects_zero_variance_feed():
    panel = pl.DataFrame(
        {
            "date": [1, 2, 3],
            "symbol": ["A", "B", "C"],
            "net_moneyflow_ratio": [0.1, 0.1, 0.1],
            "large_order_ratio": [0.2, 0.2, 0.2],
        }
    )

    result = validate_feature_panel(
        panel,
        ["net_moneyflow_ratio", "large_order_ratio"],
    )

    assert not result.passed
    assert result.moneyflow_status == "degenerate"
    assert any("near-zero" in failure for failure in result.failures)


def test_evidently_drift_report_uses_current_metric_schema(tmp_path):
    reference = pl.DataFrame(
        {
            "momentum": [float(index) for index in range(100)],
            "volume_ratio": [float(index % 5) for index in range(100)],
        }
    )
    current = reference.with_columns((pl.col("momentum") + 0.1).alias("momentum"))
    output = tmp_path / "drift.json"

    result = run_evidently_drift(
        reference,
        current,
        ["momentum", "volume_ratio"],
        output,
    )

    assert result["passed"]
    assert result["drifted_feature_share"] == 0
    assert result["reference_rows"] == 100
    assert set(result["feature_scores"]) == {"momentum", "volume_ratio"}
    assert json.loads(output.read_text("utf-8"))["passed"] is True


def test_latest_drift_windows_do_not_overlap(monkeypatch, tmp_path):
    panel = pl.DataFrame(
        {
            "date": [index for index in range(10)],
            "symbol": ["A"] * 10,
            "feature": [float(index) for index in range(10)],
        }
    )
    captured = {}

    def fake(reference, current, feature_names, output_path, maximum_drifted_share):
        captured["reference"] = reference["date"].to_list()
        captured["current"] = current["date"].to_list()
        return {"passed": True}

    monkeypatch.setattr("ashare_research.quality.run_evidently_drift", fake)
    run_latest_evidently_drift(
        panel,
        ["feature"],
        tmp_path / "drift.json",
        reference_bars=6,
        current_bars=4,
    )

    assert captured["reference"] == list(range(6))
    assert captured["current"] == list(range(6, 10))
