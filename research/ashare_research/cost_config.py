"""Unified A-share transaction cost model shared by TypeScript (web) and Python (research).

Reads from ``config/cost-model.json`` so both languages use the same economic
contract.  The file is resolved relative to the repo root.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


@dataclass(frozen=True)
class CostModel:
    buy_commission_bps: float
    sell_commission_bps: float
    stamp_duty_bps: float
    base_slippage_bps: float
    minimum_commission_yuan: float
    impact_model: str

    @property
    def buy_total_bps(self) -> float:
        return self.buy_commission_bps + self.base_slippage_bps

    @property
    def sell_total_bps(self) -> float:
        return self.sell_commission_bps + self.stamp_duty_bps + self.base_slippage_bps

    @property
    def round_trip_bps(self) -> float:
        return self.buy_total_bps + self.sell_total_bps

    @property
    def avg_side_bps(self) -> float:
        """Legacy ``fee_bps`` per-side equivalent (avg of buy and sell)."""
        return self.round_trip_bps / 2


def _repo_root() -> Path:
    # research/ashare_research/cost_config.py → research/ashare_research → research → repo root
    return Path(__file__).resolve().parents[2]


@lru_cache(maxsize=1)
def load_cost_model() -> CostModel:
    fp = _repo_root() / "config" / "cost-model.json"
    raw = json.loads(fp.read_text("utf-8"))
    return CostModel(
        buy_commission_bps=float(raw["buy_commission_bps"]),
        sell_commission_bps=float(raw["sell_commission_bps"]),
        stamp_duty_bps=float(raw["stamp_duty_bps"]),
        base_slippage_bps=float(raw["base_slippage_bps"]),
        minimum_commission_yuan=float(raw.get("minimum_commission_yuan", 5.0)),
        impact_model=str(raw.get("impact_model", "sqrt-volume")),
    )
