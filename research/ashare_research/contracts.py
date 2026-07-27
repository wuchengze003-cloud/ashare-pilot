"""Stable contracts shared by data, training, inference and the web adapter."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from typing import Any, Literal

HORIZONS = (1, 3, 5, 10)
Action = Literal["buy", "hold", "sell", "cash"]
TradeSide = Literal["buy", "sell", "reduce"]
ModelStage = Literal["candidate", "shadow", "champion", "retired"]


@dataclass(frozen=True)
class PriceBar:
    date: date
    symbol: str
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True)
class PredictionRecord:
    decision_date: date
    data_cutoff: date
    symbol: str
    model_version: str
    feature_version: str
    horizon_bars: int
    expected_return: float
    downside_return: float
    confidence: float
    rank: int
    target_weight: float
    action: Action
    reason_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.horizon_bars not in HORIZONS:
            raise ValueError(f"unsupported horizon: {self.horizon_bars}")
        if self.data_cutoff > self.decision_date:
            raise ValueError("prediction data_cutoff cannot exceed decision_date")
        if not 0 <= self.confidence <= 1:
            raise ValueError("confidence must be between 0 and 1")
        if not 0 <= self.target_weight <= 1:
            raise ValueError("target_weight must be between 0 and 1")


@dataclass(frozen=True)
class DecisionEvent:
    decision_date: date
    model_version: str
    symbol: str
    action: Action
    rank: int
    target_weight_before: float
    target_weight_after: float
    expected_return: float
    downside_risk: float
    reason_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExecutionEvent:
    decision_date: date
    trade_date: date
    model_version: str
    symbol: str
    side: TradeSide
    shares: int
    price: float
    fee: float
    price_field: Literal["open"] = "open"
    rejected_reason: str | None = None


@dataclass(frozen=True)
class OutcomeRecord:
    decision_date: date
    symbol: str
    model_version: str
    horizon_bars: int
    entry_date: date
    entry_open: float
    evaluation_date: date
    exit_close: float
    net_return: float
    benchmark_return: float | None
    excess_return: float | None
    mfe: float
    mae: float
    opportunity_cost: float


@dataclass(frozen=True)
class ModelMetrics:
    primary_sharpe: float
    oos_sharpe: float
    max_drawdown_pct: float
    double_cost_return_pct: float
    average_hold_bars: float
    turnover_pct: float
    bootstrap_win_probability: float
    shadow_trading_days: int
    closed_trades: int
    oos_folds: int
    data_quality_passed: bool
    drift_passed: bool


@dataclass(frozen=True)
class ModelManifest:
    model_version: str
    feature_version: str
    model_type: Literal["linear", "lightgbm", "double_ensemble"]
    stage: ModelStage
    created_at: datetime
    data_cutoff: date
    artifact_uri: str
    primary_window: str
    data_source: Literal["tushare-pro-point-in-time", "community-qlib-cold-start"]
    promotable: bool
    artifact_sha256: str = ""
    oos_evaluation_uri: str = ""
    oos_evaluation_sha256: str = ""
    holdout_evaluation_uri: str = ""
    holdout_evaluation_sha256: str = ""
    training_windows: tuple[dict[str, str], ...] = ()
    params: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PromotionEvidence:
    model_version: str
    evaluated_at: datetime
    stage: Literal["shadow"]
    primary_window: str
    metrics_source: Literal["system-evaluation-v2"]
    metrics: ModelMetrics
    source_uris: dict[str, str] = field(default_factory=dict)
    source_hashes: dict[str, str] = field(default_factory=dict)
