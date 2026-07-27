"""Data quality checks and Evidently drift reports."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path

import polars as pl

DEFAULT_FEATURE_MISSING_RATE_LIMITS = {
    # Tushare leaves pe_ttm null for loss-making companies. Across the
    # point-in-time A-share panel this structural absence is stable at
    # roughly 24.7%-28.3%, so PE gets an explicit limit without weakening
    # the 25% default applied to every other feature.
    "log_pe_ttm": 0.30,
}


@dataclass(frozen=True)
class DataQualityResult:
    passed: bool
    rows: int
    duplicate_keys: int
    future_rows: int
    missing_feature_rates: dict[str, float]
    missing_feature_limits: dict[str, float]
    failures: tuple[str, ...]


def validate_feature_panel(
    panel: pl.DataFrame,
    feature_names: list[str],
    decision_date: str | None = None,
    max_missing_rate: float = 0.25,
    feature_missing_rate_limits: Mapping[str, float] | None = None,
) -> DataQualityResult:
    if not 0 <= max_missing_rate <= 1:
        raise ValueError("max_missing_rate must be between 0 and 1")
    configured_limits = dict(DEFAULT_FEATURE_MISSING_RATE_LIMITS)
    if feature_missing_rate_limits is not None:
        configured_limits.update(feature_missing_rate_limits)
    limits = {
        name: float(configured_limits.get(name, max_missing_rate))
        for name in feature_names
    }
    if any(not 0 <= limit <= 1 for limit in limits.values()):
        raise ValueError("feature missing-rate limits must be between 0 and 1")

    duplicate_keys = panel.select(pl.struct("date", "symbol").is_duplicated().sum()).item()
    future_rows = 0
    if decision_date is not None:
        future_rows = panel.filter(
            pl.col("date") > pl.lit(decision_date).str.strptime(pl.Date, "%Y-%m-%d")
        ).height
    missing: dict[str, float] = {}
    for name in feature_names:
        values = panel[name].cast(pl.Float64, strict=False)
        missing_count = int(values.is_null().sum()) + int(
            values.is_nan().fill_null(False).sum()
        )
        missing[name] = float(missing_count / max(panel.height, 1))
    failures = []
    if duplicate_keys:
        failures.append(f"duplicate date/symbol keys: {duplicate_keys}")
    if future_rows:
        failures.append(f"rows after decision date: {future_rows}")
    excessive = [
        name for name, rate in missing.items() if rate > limits[name]
    ]
    if excessive:
        failures.append(f"features above missing threshold: {','.join(excessive)}")
    return DataQualityResult(
        passed=not failures,
        rows=panel.height,
        duplicate_keys=duplicate_keys,
        future_rows=future_rows,
        missing_feature_rates=missing,
        missing_feature_limits=limits,
        failures=tuple(failures),
    )


def _find_drift_share(value) -> float | None:
    if isinstance(value, dict):
        config = value.get("config")
        metric_value = value.get("value")
        if (
            isinstance(config, dict)
            and str(config.get("type", "")).endswith("DriftedColumnsCount")
            and isinstance(metric_value, dict)
            and isinstance(metric_value.get("share"), int | float)
        ):
            return float(metric_value["share"])
        for key, item in value.items():
            if "share_of_drifted" in key and isinstance(item, int | float):
                return float(item)
            found = _find_drift_share(item)
            if found is not None:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find_drift_share(item)
            if found is not None:
                return found
    return None


def _feature_drift_scores(payload: dict) -> dict[str, float]:
    scores: dict[str, float] = {}
    for metric in payload.get("metrics", []):
        if not isinstance(metric, dict):
            continue
        config = metric.get("config")
        value = metric.get("value")
        if (
            isinstance(config, dict)
            and str(config.get("type", "")).endswith("ValueDrift")
            and isinstance(config.get("column"), str)
            and isinstance(value, int | float)
        ):
            scores[config["column"]] = float(value)
    return scores


def run_evidently_drift(
    reference: pl.DataFrame,
    current: pl.DataFrame,
    feature_names: list[str],
    output_path: Path | str,
    maximum_drifted_share: float = 0.3,
) -> dict:
    from evidently import Report
    from evidently.presets import DataDriftPreset

    report = Report(
        [DataDriftPreset(method="psi", drift_share=maximum_drifted_share)],
        include_tests=True,
    )
    snapshot = report.run(
        current.select(feature_names).to_pandas(), reference.select(feature_names).to_pandas()
    )
    payload = json.loads(snapshot.json())
    drifted_share = _find_drift_share(payload)
    if drifted_share is None:
        raise RuntimeError("Evidently result did not contain drifted feature share")
    result = {
        "passed": drifted_share <= maximum_drifted_share,
        "drifted_feature_share": drifted_share,
        "maximum_drifted_share": maximum_drifted_share,
        "reference_rows": reference.height,
        "current_rows": current.height,
        "feature_scores": _feature_drift_scores(payload),
        "report": payload,
    }
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", "utf-8")
    return result


def run_latest_evidently_drift(
    panel: pl.DataFrame,
    feature_names: list[str],
    output_path: Path | str,
    reference_bars: int = 120,
    current_bars: int = 20,
    maximum_drifted_share: float = 0.3,
) -> dict:
    if reference_bars <= 0 or current_bars <= 0:
        raise ValueError("drift window bars must be positive")
    dates = panel.select("date").unique().sort("date")["date"].to_list()
    required = reference_bars + current_bars
    if len(dates) < required:
        raise ValueError(f"drift requires {required} dates, panel has {len(dates)}")
    current_dates = dates[-current_bars:]
    reference_dates = dates[-required:-current_bars]
    return run_evidently_drift(
        panel.filter(pl.col("date").is_in(reference_dates)),
        panel.filter(pl.col("date").is_in(current_dates)),
        feature_names,
        output_path,
        maximum_drifted_share,
    )


def write_quality_result(result: DataQualityResult, output_path: Path | str) -> None:
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(asdict(result), ensure_ascii=False, indent=2) + "\n", "utf-8")
