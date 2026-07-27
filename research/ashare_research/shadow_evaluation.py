"""Evaluate realized shadow-account equity and execution history."""

from __future__ import annotations

import json
from datetime import date
from math import sqrt
from pathlib import Path
from statistics import fmean, pstdev

from .evaluation import file_sha256
from .ledger import read_executions


def evaluate_shadow_account(
    state_path: Path | str,
    ledger_path: Path | str,
) -> dict:
    state_path = Path(state_path)
    state = json.loads(state_path.read_text("utf-8"))
    curve = state.get("equity_curve", [])
    start_cash = float(state["start_cash"])
    equities = [start_cash, *[float(point["equity"]) for point in curve]]
    returns = [equities[index] / equities[index - 1] - 1 for index in range(1, len(equities))]
    volatility = pstdev(returns) if len(returns) > 1 else 0.0
    sharpe = fmean(returns) / volatility * sqrt(252) if volatility > 0 else 0.0
    peak = equities[0]
    max_drawdown = 0.0
    for equity in equities:
        peak = max(peak, equity)
        max_drawdown = min(max_drawdown, equity / peak - 1)
    tail_count = max(1, int(len(returns) * 0.05))
    cvar = fmean(sorted(returns)[:tail_count]) if returns else 0.0

    executions = [
        event
        for event in read_executions(ledger_path, state["model_version"])
        if not event.rejected_reason and event.shares > 0
    ]
    shares: dict[str, int] = {}
    entry_date: dict[str, date] = {}
    holds: list[int] = []
    curve_dates = [date.fromisoformat(point["date"]) for point in curve]
    date_index = {value: index for index, value in enumerate(curve_dates)}
    turnover = 0.0
    closed = 0
    for event in executions:
        turnover += event.shares * event.price
        if event.side == "buy":
            if shares.get(event.symbol, 0) == 0:
                entry_date[event.symbol] = event.trade_date
            shares[event.symbol] = shares.get(event.symbol, 0) + event.shares
        else:
            shares[event.symbol] = max(0, shares.get(event.symbol, 0) - event.shares)
            if shares[event.symbol] == 0 and event.symbol in entry_date:
                entered = entry_date.pop(event.symbol)
                if event.trade_date in date_index and entered in date_index:
                    holds.append(date_index[event.trade_date] - date_index[entered] + 1)
                closed += 1
    if curve:
        final_date = date.fromisoformat(curve[-1]["date"])
        for symbol, entered in entry_date.items():
            if shares.get(symbol, 0) > 0:
                before = sum(1 for point in curve if date.fromisoformat(point["date"]) < entered)
                after = sum(1 for point in curve if date.fromisoformat(point["date"]) <= final_date)
                holds.append(max(1, after - before))
    average_equity = fmean(equities)
    portfolio = {
        "total_return_pct": (equities[-1] / equities[0] - 1) * 100 if equities else 0,
        "sharpe": sharpe,
        "max_drawdown_pct": max_drawdown * 100,
        "cvar_5_pct": cvar * 100,
        "turnover_pct": turnover / average_equity * 100 if average_equity else 0,
        "average_hold_bars": fmean(holds) if holds else 0,
        "closed_trades": closed,
        "equity_curve": curve,
        "trades": len(executions),
    }
    return {
        "schema_version": 2,
        "evaluation_engine": "ashare-shadow-next-open-v2",
        "model_version": state["model_version"],
        "data_cutoff": state.get("last_mark_date"),
        "source_sha256": file_sha256(state_path),
        "oos_folds": 0,
        "signal": {},
        "portfolio": portfolio,
        "double_cost_portfolio": portfolio,
    }


def write_shadow_evaluation(report: dict, output_path: Path | str) -> None:
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", "utf-8")
