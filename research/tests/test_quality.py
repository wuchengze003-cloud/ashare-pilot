import json

import polars as pl

from ashare_research.quality import run_evidently_drift, run_latest_evidently_drift


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
