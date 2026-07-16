"""Deterministic champion health checks and rollback triggers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ModelHealth:
    consecutive_underperform_days: int
    current_drawdown_pct: float
    data_quality_passed: bool
    drift_passed: bool


def rollback_reasons(active: dict[str, Any], health: ModelHealth) -> tuple[str, ...]:
    reasons: list[str] = []
    drawdown_limit = float(active.get("rollback_drawdown_limit_pct", 15.0))
    if health.consecutive_underperform_days >= 10:
        reasons.append("UNDERPERFORMED_10_DAYS")
    if abs(min(health.current_drawdown_pct, 0.0)) > drawdown_limit:
        reasons.append("DRAWDOWN_GUARD_BREACHED")
    if not health.data_quality_passed:
        reasons.append("DATA_QUALITY_FAILED")
    if not health.drift_passed:
        reasons.append("DRIFT_FAILED")
    return tuple(reasons)
