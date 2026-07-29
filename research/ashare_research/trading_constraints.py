"""Unified trading-constraints reader for the Python/Research side.

Reads from config/trading-constraints.json so Web and Research share the
same board rules, buy constraints, and risk parameters.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def _resolve_config_path() -> Path:
    # research/ashare_research/trading_constraints.py → research → repo root
    return Path(__file__).resolve().parents[2] / "config" / "trading-constraints.json"


@dataclass(frozen=True)
class TradingConstraints:
    allowed_boards: list[str]
    price_limit_fractions: dict[str, float]
    symbol_prefixes: dict[str, list[str]]
    exclude_limit_up: bool
    limit_slack: float
    max_one_day_return_to_buy: float
    initial_capital_yuan: float
    max_drawdown_pct: float
    max_single_position_pct: float
    max_single_theme_pct: float
    max_positions: int
    supported_signal_frequencies: list[str]
    intraday_execution_price: str
    t_plus_1: bool
    lot_size: int
    max_order_bar_amount_pct: float
    min_holding_bars: int
    rebalance_threshold_pct: float
    execution_price: str
    decision_timing: str
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def round_trip_fee_bps(self) -> float:
        """Alias for compatibility, derived from the shared cost model."""
        from .cost_config import load_cost_model

        return load_cost_model().round_trip_bps


_cached: TradingConstraints | None = None


def load_trading_constraints() -> TradingConstraints:
    global _cached
    if _cached is not None:
        return _cached
    data = json.loads(_resolve_config_path().read_text("utf-8"))
    _cached = TradingConstraints(
        allowed_boards=data["boards"]["allowed"],
        price_limit_fractions=data["boards"]["price_limit_fractions"],
        symbol_prefixes=data["boards"]["symbol_prefixes"],
        exclude_limit_up=data["buy_constraints"]["exclude_limit_up"],
        limit_slack=data["buy_constraints"]["limit_slack"],
        max_one_day_return_to_buy=data["buy_constraints"]["max_one_day_return_to_buy"],
        initial_capital_yuan=data["risk_management"]["initial_capital_yuan"],
        max_drawdown_pct=data["risk_management"]["max_drawdown_pct"],
        max_single_position_pct=data["risk_management"]["max_single_position_pct"],
        max_single_theme_pct=data["risk_management"]["max_single_theme_pct"],
        max_positions=data["risk_management"]["max_positions"],
        supported_signal_frequencies=data["execution"][
            "supported_signal_frequencies"
        ],
        intraday_execution_price=data["execution"]["intraday_execution_price"],
        t_plus_1=bool(data["execution"]["t_plus_1"]),
        lot_size=int(data["execution"]["lot_size"]),
        max_order_bar_amount_pct=float(
            data["execution"]["max_order_bar_amount_pct"]
        ),
        min_holding_bars=data["execution"]["min_holding_bars"],
        rebalance_threshold_pct=data["execution"]["rebalance_threshold_pct"],
        execution_price=data["execution"]["execution_price"],
        decision_timing=data["execution"]["decision_timing"],
        raw=data,
    )
    return _cached


def price_limit_fraction(symbol: str, name: str = "") -> float:
    """A-share daily price-limit fraction by board."""
    cfg = load_trading_constraints()
    code = re.sub(r"^(sh|sz|bj)", "", symbol, flags=re.IGNORECASE)
    code = re.sub(r"\.(sh|sz|bj)$", "", code, flags=re.IGNORECASE)
    for board in ("star", "chinext", "bse"):
        prefixes = cfg.symbol_prefixes.get(board, [])
        if any(code.startswith(p) for p in prefixes):
            return cfg.price_limit_fractions[board]
    if re.search(r"ST", name, re.IGNORECASE):
        return cfg.price_limit_fractions["main_st"]
    return cfg.price_limit_fractions["main"]


def is_limit_up(close: float, prev_close: float, symbol: str, name: str = "") -> bool:
    cfg = load_trading_constraints()
    frac = price_limit_fraction(symbol, name)
    if prev_close <= 0:
        return False
    return close / prev_close - 1 >= frac - cfg.limit_slack
