"""Generate deterministic champion health evidence for rollback checks."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path


def _read(path: Path) -> dict:
    return json.loads(path.read_text("utf-8"))


def _returns(curve: list[dict], start_equity: float | None = None) -> dict[str, float]:
    result: dict[str, float] = {}
    previous = start_equity
    for point in sorted(curve, key=lambda value: value["date"]):
        equity = float(point["equity"])
        if previous is not None and previous > 0:
            result[str(point["date"])] = equity / previous - 1
        previous = equity
    return result


def build_model_health(
    state_path: Path | str,
    baseline_evaluation_path: Path | str,
    quality_path: Path | str,
    drift_path: Path | str,
    as_of: str,
    underperformance_margin: float = 0.0025,
) -> dict:
    state = _read(Path(state_path))
    baseline = _read(Path(baseline_evaluation_path))
    quality = _read(Path(quality_path))
    drift = _read(Path(drift_path))
    curve = state.get("equity_curve", [])
    active_returns = _returns(curve, float(state["start_cash"]))
    baseline_returns = _returns(baseline["portfolio"]["equity_curve"])
    overlapping = sorted(set(active_returns) & set(baseline_returns))
    consecutive = 0
    for value in reversed(overlapping):
        if active_returns[value] < baseline_returns[value] - underperformance_margin:
            consecutive += 1
        else:
            break

    peak = float(state["start_cash"])
    current_drawdown = 0.0
    for point in curve:
        equity = float(point["equity"])
        peak = max(peak, equity)
        current_drawdown = equity / peak - 1 if peak > 0 else 0.0

    result = {
        "generated_at": datetime.now(UTC).isoformat(),
        "as_of": as_of,
        "model_version": state["model_version"],
        "consecutive_underperform_days": consecutive,
        "underperformance_margin": underperformance_margin,
        "overlapping_return_days": len(overlapping),
        "current_drawdown_pct": current_drawdown * 100,
        "data_quality_passed": bool(quality.get("passed", False)),
        "drift_passed": bool(drift.get("passed", False)),
    }
    return result


def write_model_health(result: dict, output_path: Path | str) -> None:
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", "utf-8")
