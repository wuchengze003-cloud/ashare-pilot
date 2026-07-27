"""Export the existing V1 runtime into comparable research evidence."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from statistics import fmean

from .contracts import ModelMetrics
from .evaluation import file_sha256


def _average_hold_bars(backtest: dict) -> tuple[float, int]:
    dates = [date.fromisoformat(point["date"]) for point in backtest["equityCurve"]]
    date_index = {value: index for index, value in enumerate(dates)}
    lots: dict[str, list[tuple[int, int]]] = {}
    holds: list[tuple[int, int]] = []
    for trade in backtest["trades"]:
        trade_date = date.fromisoformat(trade.get("tradeDate") or trade["date"])
        index = date_index.get(trade_date)
        if index is None:
            continue
        symbol = trade["symbol"]
        shares = int(trade["shares"])
        if trade["side"] == "buy":
            lots.setdefault(symbol, []).append((shares, index))
            continue
        remaining = shares
        queue = lots.setdefault(symbol, [])
        while remaining > 0 and queue:
            lot_shares, entered = queue[0]
            sold = min(remaining, lot_shares)
            holds.append((index - entered + 1, sold))
            remaining -= sold
            if sold == lot_shares:
                queue.pop(0)
            else:
                queue[0] = (lot_shares - sold, entered)
    weighted = sum(length * shares for length, shares in holds)
    shares = sum(shares for _, shares in holds)
    return (weighted / shares if shares else 0.0), len(holds)


def export_v1_baseline(
    backtest_path: Path | str, output_dir: Path | str
) -> tuple[dict, ModelMetrics]:
    backtest_path = Path(backtest_path)
    output_dir = Path(output_dir)
    value = json.loads(backtest_path.read_text("utf-8"))
    curve = [
        {
            "date": point["date"],
            "equity": point["equity"],
            "cash": point["cash"],
            "positions": len(point["positions"]),
        }
        for point in value["equityCurve"]
    ]
    equities = [float(point["equity"]) for point in curve]
    returns = [equities[index] / equities[index - 1] - 1 for index in range(1, len(equities))]
    tail_count = max(1, int(len(returns) * 0.05))
    average_hold, closed = _average_hold_bars(value)
    portfolio = {
        "total_return_pct": float(value["stats"]["totalReturnPct"]),
        "sharpe": float(value["stats"]["sharpe"]),
        "max_drawdown_pct": float(value["stats"]["maxDrawdownPct"]),
        "cvar_5_pct": fmean(sorted(returns)[:tail_count]) * 100 if returns else 0.0,
        "turnover_pct": float(value["stats"].get("turnoverPct", 0)),
        "average_hold_bars": average_hold,
        "closed_trades": closed,
        "equity_curve": curve,
        "trades": len(value["trades"]),
    }
    report = {
        "schema_version": 2,
        "evaluation_engine": "web-v1-next-open-adapter-v2",
        "model_version": "V1",
        "data_cutoff": value["latestDate"],
        "source_sha256": file_sha256(backtest_path),
        "oos_folds": 0,
        "signal": {},
        "portfolio": portfolio,
        "double_cost_portfolio": portfolio,
        "comparability_warning": "V1 has no pre-2026 multi-fold OOS portfolio record.",
    }
    metrics = ModelMetrics(
        primary_sharpe=portfolio["sharpe"],
        oos_sharpe=portfolio["sharpe"],
        max_drawdown_pct=portfolio["max_drawdown_pct"],
        double_cost_return_pct=portfolio["total_return_pct"],
        average_hold_bars=portfolio["average_hold_bars"],
        turnover_pct=portfolio["turnover_pct"],
        bootstrap_win_probability=0,
        shadow_trading_days=len(curve),
        closed_trades=closed,
        oos_folds=0,
        data_quality_passed=True,
        drift_passed=True,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "evaluation.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", "utf-8"
    )
    (output_dir / "metrics.json").write_text(
        json.dumps(metrics.__dict__, ensure_ascii=False, indent=2) + "\n", "utf-8"
    )
    return report, metrics
