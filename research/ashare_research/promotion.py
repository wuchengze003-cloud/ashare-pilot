"""Deterministic champion/challenger promotion gates."""

from __future__ import annotations

from dataclasses import dataclass

from .contracts import ModelManifest, ModelMetrics, PromotionEvidence


@dataclass(frozen=True)
class GateFailure:
    code: str
    actual: float | int | bool | str
    required: float | int | bool | str


@dataclass(frozen=True)
class GateResult:
    passed: bool
    failures: tuple[GateFailure, ...]


def evaluate_promotion(
    candidate: ModelManifest,
    evidence: PromotionEvidence,
    champion: ModelMetrics,
) -> GateResult:
    metrics = evidence.metrics
    failures: list[GateFailure] = []

    def require(code: str, condition: bool, actual: object, required: object) -> None:
        if not condition:
            failures.append(GateFailure(code, actual, required))

    require(
        "MODEL_VERSION_MATCH",
        evidence.model_version == candidate.model_version,
        evidence.model_version,
        candidate.model_version,
    )
    require(
        "SYSTEM_EVIDENCE",
        evidence.metrics_source == "system-evaluation-v2",
        evidence.metrics_source,
        "system-evaluation-v2",
    )
    require("PROMOTABLE_MODEL", candidate.promotable, candidate.promotable, True)
    require(
        "PRODUCTION_DATA_SOURCE",
        candidate.data_source == "tushare-pro-point-in-time",
        candidate.data_source,
        "tushare-pro-point-in-time",
    )

    require("PRIMARY_SHARPE", metrics.primary_sharpe >= 3, metrics.primary_sharpe, ">=3")
    require(
        "OOS_SHARPE_IMPROVEMENT",
        metrics.oos_sharpe >= champion.oos_sharpe + 0.25,
        metrics.oos_sharpe,
        f">={champion.oos_sharpe + 0.25:.4f}",
    )
    champion_drawdown = abs(min(champion.max_drawdown_pct, 0))
    allowed_drawdown = min(15.0, champion_drawdown * 0.8) if champion_drawdown else 15.0
    candidate_drawdown = abs(min(metrics.max_drawdown_pct, 0))
    require(
        "MAX_DRAWDOWN",
        candidate_drawdown <= allowed_drawdown,
        candidate_drawdown,
        f"<={allowed_drawdown:.4f}",
    )
    require(
        "DOUBLE_COST_RETURN",
        metrics.double_cost_return_pct > 0,
        metrics.double_cost_return_pct,
        ">0",
    )
    require("AVERAGE_HOLD", metrics.average_hold_bars >= 5, metrics.average_hold_bars, ">=5")
    turnover_limit = champion.turnover_pct * 1.2
    require(
        "TURNOVER",
        metrics.turnover_pct <= turnover_limit,
        metrics.turnover_pct,
        f"<={turnover_limit:.4f}",
    )
    require(
        "BOOTSTRAP_WIN_PROBABILITY",
        metrics.bootstrap_win_probability >= 0.8,
        metrics.bootstrap_win_probability,
        ">=0.8",
    )
    require("SHADOW_DAYS", metrics.shadow_trading_days >= 60, metrics.shadow_trading_days, ">=60")
    require("CLOSED_TRADES", metrics.closed_trades >= 20, metrics.closed_trades, ">=20")
    require("OOS_FOLDS", metrics.oos_folds >= 6, metrics.oos_folds, ">=6")
    require("DATA_QUALITY", metrics.data_quality_passed, metrics.data_quality_passed, True)
    require("DRIFT", metrics.drift_passed, metrics.drift_passed, True)
    require(
        "PRIMARY_WINDOW",
        evidence.primary_window == "post_cny_2026",
        evidence.primary_window,
        "post_cny_2026",
    )
    require("SHADOW_STAGE", evidence.stage == "shadow", evidence.stage, "shadow")
    return GateResult(not failures, tuple(failures))
