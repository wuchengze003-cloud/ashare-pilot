"""Pre-registered production strategy race contract."""

from __future__ import annotations

import hashlib
import itertools
import json
import math
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from .trading_constraints import load_trading_constraints

RESEARCH_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = RESEARCH_ROOT.parent
MINUTE_PARAMETER_NAMES = {
    "confirmation_bars",
    "vwap_buffer_bps",
    "maximum_chase_pct",
    "hard_stop_pct",
    "trailing_stop_pct",
    "minimum_volume_ratio",
    "rebound_from_low_pct",
}


@dataclass(frozen=True)
class RaceWindow:
    start: date
    end: date | None

    def contains(self, value: date) -> bool:
        return value >= self.start and (self.end is None or value <= self.end)


@dataclass(frozen=True)
class CandidateSpec:
    candidate_id: str
    family: str
    description: str
    signal_frequency: str
    calibration_label: str
    label_horizon_bars: int
    parameter_grid: dict[str, tuple[float, ...]]
    minute_parameter_grid: dict[str, tuple[float, ...]]

    def parameter_sets(self) -> tuple[dict[str, float], ...]:
        return self._parameter_sets(self.parameter_grid)

    def minute_parameter_sets(self) -> tuple[dict[str, float], ...]:
        if not self.minute_parameter_grid:
            return ()
        return self._parameter_sets(self.minute_parameter_grid)

    @property
    def requires_minute(self) -> bool:
        return self.signal_frequency == "1d+5min"

    @staticmethod
    def _parameter_sets(
        grid: dict[str, tuple[float, ...]],
    ) -> tuple[dict[str, float], ...]:
        names = tuple(sorted(grid))
        return tuple(
            dict(zip(names, values, strict=True))
            for values in itertools.product(
                *(grid[name] for name in names)
            )
        )


@dataclass(frozen=True)
class PromotionGates:
    maximum_drawdown_pct: float
    minimum_validation_sharpe: float
    minimum_oos_folds: int
    minimum_closed_trades: int
    minimum_oos_sharpe: float
    minimum_frozen_sharpe: float
    minimum_median_oos_fold_sharpe: float
    minimum_positive_oos_fold_share: float
    minimum_oos_annualized_return_pct: float
    minimum_oos_calmar: float
    minimum_upside_capture: float
    maximum_downside_capture: float
    minimum_bootstrap_probability_sharpe_positive: float
    double_cost_total_return_must_be_positive: bool
    minimum_double_cost_oos_sharpe: float
    minimum_validation_trading_days: int
    minimum_oos_trading_days: int
    minimum_frozen_trading_days: int
    all_required_data_quality_gates_must_pass: bool
    minute_overlay_must_not_reduce_sharpe: bool


@dataclass(frozen=True)
class RaceConfig:
    source_path: Path
    schema: str
    version: str
    objective: str
    capital_yuan: float
    champion_slots: int
    minimum_complete_daily_trading_days: int
    historical_universe_file: Path
    production_universe_file: Path
    minimum_calibrated_net_return: float
    ranking_switch_buffer: float
    windows: dict[str, RaceWindow]
    candidates: tuple[CandidateSpec, ...]
    calibration_deciles: int
    calibration_minimum_observations: int
    validation_fold_trading_days: int
    oos_fold_trading_days: int
    promotion_gates: PromotionGates
    bootstrap_seed: int
    bootstrap_samples: int
    bootstrap_block_trading_days: int
    raw: dict[str, Any]


def _finite_number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{field} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{field} must be finite")
    return result


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _resolve_research_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else (RESEARCH_ROOT / path).resolve()


def _window(value: dict[str, Any], name: str) -> RaceWindow:
    start = date.fromisoformat(str(value["start"]))
    raw_end = str(value["end"])
    end = None if raw_end == "latest" else date.fromisoformat(raw_end)
    if end is not None and end < start:
        raise ValueError(f"window {name} ends before it starts")
    return RaceWindow(start, end)


def _validate_window_order(windows: dict[str, RaceWindow]) -> None:
    required = ("development", "validation", "oos", "frozen")
    if tuple(windows) != required:
        raise ValueError(f"windows must be ordered as {required}")
    for left_name, right_name in zip(required, required[1:], strict=False):
        left = windows[left_name]
        right = windows[right_name]
        if left.end is None or left.end >= right.start:
            raise ValueError(f"windows overlap: {left_name} and {right_name}")
    if windows["frozen"].end is not None:
        raise ValueError("frozen window must end at latest")


def _parameter_grid(
    value: Any,
    candidate_id: str,
    *,
    required: bool = True,
) -> dict[str, tuple[float, ...]]:
    if not required and value in (None, {}):
        return {}
    if not isinstance(value, dict) or not value:
        raise ValueError(f"{candidate_id}.parameter_grid must be a non-empty object")
    result: dict[str, tuple[float, ...]] = {}
    for name, raw_values in value.items():
        if not isinstance(raw_values, list) or not raw_values:
            raise ValueError(f"{candidate_id}.{name} must be a non-empty list")
        values = tuple(
            _finite_number(item, f"{candidate_id}.{name}") for item in raw_values
        )
        if len(set(values)) != len(values):
            raise ValueError(f"{candidate_id}.{name} contains duplicate values")
        result[str(name)] = values
    return result


def load_race_config(path: Path | str) -> RaceConfig:
    source = Path(path).resolve()
    raw = json.loads(source.read_text("utf-8"))
    schema = str(raw.get("$schema"))
    if schema not in {
        "ashare-production-race/v1",
        "ashare-production-race/v2",
    }:
        raise ValueError("unsupported production race schema")
    if raw.get("champion_slots") != 1:
        raise ValueError("production race must expose exactly one champion slot")

    constraints = load_trading_constraints()
    capital = _finite_number(raw.get("capital_yuan"), "capital_yuan")
    if capital != constraints.initial_capital_yuan:
        raise ValueError("race capital must match shared production constraints")

    data = raw.get("data")
    if not isinstance(data, dict):
        raise ValueError("data must be an object")
    frequencies = data.get("frequency")
    if (
        not isinstance(frequencies, list)
        or not frequencies
        or len(set(frequencies)) != len(frequencies)
        or any(value not in {"1d", "5min"} for value in frequencies)
        or "1d" not in frequencies
    ):
        raise ValueError("race data frequency must contain supported daily data")
    if schema == "ashare-production-race/v1" and frequencies != ["1d", "5min"]:
        raise ValueError("v1 race must evaluate daily and 5-minute signals")

    raw_windows = raw.get("windows")
    if not isinstance(raw_windows, dict):
        raise ValueError("windows must be an object")
    windows = {
        name: _window(value, name) for name, value in raw_windows.items()
    }
    _validate_window_order(windows)

    raw_candidates = raw.get("candidates")
    if not isinstance(raw_candidates, list) or len(raw_candidates) != 3:
        raise ValueError("production race requires exactly three candidates")
    calibration = raw.get("calibration")
    if not isinstance(calibration, dict):
        raise ValueError("calibration must be an object")
    if calibration.get("method") != "train_decile_mean":
        raise ValueError("unsupported calibration method")
    default_label = str(calibration.get("label", ""))
    if schema == "ashare-production-race/v1" and default_label != "label_return_5":
        raise ValueError("v1 race label must be label_return_5")

    candidates_list: list[CandidateSpec] = []
    for value in raw_candidates:
        candidate_id = str(value["id"])
        signal_frequency = str(
            value.get(
                "signal_frequency",
                "1d+5min" if schema == "ashare-production-race/v1" else "",
            )
        )
        if signal_frequency not in {"1d", "1d+5min"}:
            raise ValueError(
                f"{candidate_id}.signal_frequency must be 1d or 1d+5min"
            )
        if signal_frequency == "1d+5min" and "5min" not in frequencies:
            raise ValueError(
                f"{candidate_id} requires 5min data not declared by the race"
            )
        calibration_label = str(
            value.get("calibration_label", default_label)
        )
        if calibration_label not in {
            "label_return_1",
            "label_return_3",
            "label_return_5",
            "label_return_10",
            "label_excess_return_1",
            "label_excess_return_3",
            "label_excess_return_5",
            "label_excess_return_10",
        }:
            raise ValueError(
                f"{candidate_id}.calibration_label is unsupported"
            )
        inferred_horizon = int(calibration_label.rsplit("_", 1)[-1])
        label_horizon = _positive_int(
            value.get("label_horizon_bars", inferred_horizon),
            f"{candidate_id}.label_horizon_bars",
        )
        if label_horizon != inferred_horizon:
            raise ValueError(
                f"{candidate_id} label horizon does not match calibration label"
            )
        minute_grid = _parameter_grid(
            value.get("minute_parameter_grid"),
            f"{candidate_id}.minute",
            required=signal_frequency == "1d+5min",
        )
        if signal_frequency == "1d" and minute_grid:
            raise ValueError(
                f"{candidate_id} daily-only candidate cannot define minute parameters"
            )
        candidates_list.append(
            CandidateSpec(
                candidate_id=candidate_id,
                family=str(value["family"]),
                description=str(value["description"]),
                signal_frequency=signal_frequency,
                calibration_label=calibration_label,
                label_horizon_bars=label_horizon,
                parameter_grid=_parameter_grid(
                    value.get("parameter_grid"), candidate_id
                ),
                minute_parameter_grid=minute_grid,
            )
        )
    candidates = tuple(candidates_list)
    ids = [candidate.candidate_id for candidate in candidates]
    families = [candidate.family for candidate in candidates]
    if len(set(ids)) != 3 or len(set(families)) != 3:
        raise ValueError("candidate ids and alpha families must be unique")
    for candidate in candidates:
        if candidate.requires_minute and (
            set(candidate.minute_parameter_grid) != MINUTE_PARAMETER_NAMES
        ):
            raise ValueError(
                f"{candidate.candidate_id} minute parameter grid must contain "
                f"{sorted(MINUTE_PARAMETER_NAMES)}"
            )
        if len(candidate.minute_parameter_sets()) > 64:
            raise ValueError(
                f"{candidate.candidate_id} minute parameter grid is too large"
            )
        holding_values = candidate.parameter_grid.get("minimum_holding_bars", ())
        if any(
            not value.is_integer()
            or value < constraints.min_holding_bars
            for value in holding_values
        ):
            raise ValueError(
                f"{candidate.candidate_id}.minimum_holding_bars must be an "
                "integer at or above the shared production minimum"
            )

    execution = raw.get("execution")
    if not isinstance(execution, dict):
        raise ValueError("execution must be an object")
    if (
        execution.get("cost_model") != "shared-production-v2"
        or execution.get("trading_constraints") != "shared-production-v2"
        or execution.get("t_plus_1") is not True
    ):
        raise ValueError("race execution must use shared production contracts")
    if execution.get("candidate_ranking_signal") != "raw_score":
        raise ValueError("race candidates must rank by raw_score")
    minimum_calibrated_net_return = _finite_number(
        execution.get("minimum_calibrated_net_return"),
        "execution.minimum_calibrated_net_return",
    )
    if minimum_calibrated_net_return < 0:
        raise ValueError(
            "minimum calibrated net return must be non-negative"
        )
    ranking_switch_buffer = _finite_number(
        execution.get("ranking_switch_buffer"),
        "execution.ranking_switch_buffer",
    )
    if not 0 <= ranking_switch_buffer <= 1:
        raise ValueError("ranking switch buffer must be between 0 and 1")

    gates = raw.get("promotion_gates")
    if not isinstance(gates, dict):
        raise ValueError("promotion_gates must be an object")
    promotion_gates = PromotionGates(
        maximum_drawdown_pct=_finite_number(
            gates["maximum_drawdown_pct"], "maximum_drawdown_pct"
        ),
        minimum_validation_sharpe=_finite_number(
            gates["minimum_validation_sharpe"], "minimum_validation_sharpe"
        ),
        minimum_oos_folds=_positive_int(
            gates["minimum_oos_folds"], "minimum_oos_folds"
        ),
        minimum_closed_trades=_positive_int(
            gates["minimum_closed_trades"], "minimum_closed_trades"
        ),
        minimum_oos_sharpe=_finite_number(
            gates["minimum_oos_sharpe"], "minimum_oos_sharpe"
        ),
        minimum_frozen_sharpe=_finite_number(
            gates["minimum_frozen_sharpe"], "minimum_frozen_sharpe"
        ),
        minimum_median_oos_fold_sharpe=_finite_number(
            gates["minimum_median_oos_fold_sharpe"],
            "minimum_median_oos_fold_sharpe",
        ),
        minimum_positive_oos_fold_share=_finite_number(
            gates["minimum_positive_oos_fold_share"],
            "minimum_positive_oos_fold_share",
        ),
        minimum_oos_annualized_return_pct=_finite_number(
            gates["minimum_oos_annualized_return_pct"],
            "minimum_oos_annualized_return_pct",
        ),
        minimum_oos_calmar=_finite_number(
            gates["minimum_oos_calmar"], "minimum_oos_calmar"
        ),
        minimum_upside_capture=_finite_number(
            gates["minimum_upside_capture"], "minimum_upside_capture"
        ),
        maximum_downside_capture=_finite_number(
            gates["maximum_downside_capture"], "maximum_downside_capture"
        ),
        minimum_bootstrap_probability_sharpe_positive=_finite_number(
            gates["minimum_bootstrap_probability_sharpe_positive"],
            "minimum_bootstrap_probability_sharpe_positive",
        ),
        double_cost_total_return_must_be_positive=bool(
            gates["double_cost_total_return_must_be_positive"]
        ),
        minimum_double_cost_oos_sharpe=_finite_number(
            gates["minimum_double_cost_oos_sharpe"],
            "minimum_double_cost_oos_sharpe",
        ),
        minimum_validation_trading_days=_positive_int(
            gates.get("minimum_validation_trading_days", 1),
            "minimum_validation_trading_days",
        ),
        minimum_oos_trading_days=_positive_int(
            gates.get("minimum_oos_trading_days", 1),
            "minimum_oos_trading_days",
        ),
        minimum_frozen_trading_days=_positive_int(
            gates.get("minimum_frozen_trading_days", 1),
            "minimum_frozen_trading_days",
        ),
        all_required_data_quality_gates_must_pass=bool(
            gates["all_required_data_quality_gates_must_pass"]
        ),
        minute_overlay_must_not_reduce_sharpe=bool(
            gates["minute_overlay_must_not_reduce_sharpe"]
        ),
    )
    if promotion_gates.maximum_drawdown_pct != constraints.max_drawdown_pct:
        raise ValueError("race drawdown gate must match shared production constraints")
    probabilities = (
        promotion_gates.minimum_positive_oos_fold_share,
        promotion_gates.minimum_upside_capture,
        promotion_gates.maximum_downside_capture,
        promotion_gates.minimum_bootstrap_probability_sharpe_positive,
    )
    if any(value < 0 or value > 1 for value in probabilities):
        raise ValueError("probability and capture gates must be between 0 and 1")

    bootstrap = raw.get("bootstrap")
    if not isinstance(bootstrap, dict):
        raise ValueError("bootstrap must be an object")
    selection = raw.get("selection")
    if not isinstance(selection, dict):
        raise ValueError("selection must be an object")
    return RaceConfig(
        source_path=source,
        schema=schema,
        version=str(raw["version"]),
        objective=str(raw["objective"]),
        capital_yuan=capital,
        champion_slots=1,
        minimum_complete_daily_trading_days=_positive_int(
            data["minimum_complete_daily_trading_days"],
            "minimum_complete_daily_trading_days",
        ),
        historical_universe_file=_resolve_research_path(
            str(data["historical_universe_file"])
        ),
        production_universe_file=_resolve_research_path(
            str(data["production_universe_file"])
        ),
        minimum_calibrated_net_return=minimum_calibrated_net_return,
        ranking_switch_buffer=ranking_switch_buffer,
        windows=windows,
        candidates=candidates,
        calibration_deciles=_positive_int(
            calibration["deciles"], "calibration.deciles"
        ),
        calibration_minimum_observations=_positive_int(
            calibration["minimum_observations_per_decile"],
            "calibration.minimum_observations_per_decile",
        ),
        validation_fold_trading_days=_positive_int(
            selection.get("validation_fold_trading_days", 126),
            "selection.validation_fold_trading_days",
        ),
        oos_fold_trading_days=_positive_int(
            selection.get("oos_fold_trading_days", 63),
            "selection.oos_fold_trading_days",
        ),
        promotion_gates=promotion_gates,
        bootstrap_seed=int(bootstrap["seed"]),
        bootstrap_samples=_positive_int(
            bootstrap["samples"], "bootstrap.samples"
        ),
        bootstrap_block_trading_days=_positive_int(
            bootstrap["block_trading_days"], "bootstrap.block_trading_days"
        ),
        raw=raw,
    )


def race_contract_sha256(config: RaceConfig) -> str:
    digest = hashlib.sha256()
    for path in (
        config.source_path,
        REPO_ROOT / "config" / "cost-model.json",
        REPO_ROOT / "config" / "trading-constraints.json",
        Path(__file__),
        RESEARCH_ROOT / "ashare_research" / "strategy_factors.py",
        RESEARCH_ROOT / "ashare_research" / "strategy_race.py",
        RESEARCH_ROOT / "ashare_research" / "portfolio.py",
        RESEARCH_ROOT / "ashare_research" / "features.py",
        RESEARCH_ROOT / "ashare_research" / "cost_config.py",
        RESEARCH_ROOT / "ashare_research" / "trading_constraints.py",
    ):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()
