"""Persistent shadow account with next-open execution and append-only trades."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import date
from pathlib import Path

import polars as pl

from .contracts import ExecutionEvent
from .ledger import append_execution, init_ledger


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name, suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _public_symbol(symbol: str) -> str:
    return symbol[2:] if symbol[:2] in {"sh", "sz", "bj"} else symbol


def load_shadow_state(path: Path | str, model_version: str, start_cash: float) -> dict:
    path = Path(path)
    if path.exists():
        state = json.loads(path.read_text("utf-8"))
        if state["model_version"] != model_version:
            raise ValueError("shadow state model_version mismatch")
        return state
    return {
        "schema_version": 1,
        "model_version": model_version,
        "start_cash": start_cash,
        "cash": start_cash,
        "positions": {},
        "position_factors": {},
        "pending": None,
        "last_mark_date": None,
        "equity_curve": [],
    }


def _latest_prices(panel: pl.DataFrame, current_date: date) -> dict[str, dict]:
    rows = panel.filter(pl.col("date") == current_date).select(
        "symbol",
        "open",
        "close",
        "adj_factor",
        "can_buy_open",
        "can_sell_open",
    )
    return {_public_symbol(row["symbol"]): row for row in rows.iter_rows(named=True)}


def advance_shadow_account(
    state_path: Path | str,
    panel: pl.DataFrame,
    ledger_path: Path | str,
    model_version: str,
    fee_bps: float = 10,
    start_cash: float = 1_000_000,
) -> dict:
    state_path = Path(state_path)
    state = load_shadow_state(state_path, model_version, start_cash)
    current_date = panel["date"].max()
    if not isinstance(current_date, date):
        raise ValueError("feature panel has no current date")
    if state["last_mark_date"] == current_date.isoformat():
        return state
    prices = _latest_prices(panel, current_date)
    positions = {symbol: int(shares) for symbol, shares in state["positions"].items()}
    position_factors = {
        symbol: float(value) for symbol, value in state.get("position_factors", {}).items()
    }
    cash = float(state["cash"])
    fee_rate = fee_bps / 10_000
    pending = state.get("pending")
    init_ledger(ledger_path)

    for symbol, shares in list(positions.items()):
        quote = prices.get(symbol)
        if not quote:
            continue
        current_factor = float(quote["adj_factor"])
        previous_factor = position_factors.get(symbol, current_factor)
        if previous_factor > 0 and abs(current_factor / previous_factor - 1) > 1e-10:
            positions[symbol] = int(round(shares * current_factor / previous_factor))
        position_factors[symbol] = current_factor

    if pending and pending["decision_date"] < current_date.isoformat():
        open_equity = cash + sum(
            shares * float(prices[symbol]["open"])
            for symbol, shares in positions.items()
            if symbol in prices
        )
        targets = {str(symbol): float(weight) for symbol, weight in pending["targets"].items()}
        for symbol, shares in sorted(list(positions.items())):
            quote = prices.get(symbol)
            if not quote:
                continue
            target_shares = int(
                (open_equity * targets.get(symbol, 0.0) / float(quote["open"])) // 100 * 100
            )
            sell_shares = max(0, shares - target_shares)
            if sell_shares == 0:
                continue
            if not quote["can_sell_open"]:
                append_execution(
                    ledger_path,
                    ExecutionEvent(
                        decision_date=date.fromisoformat(pending["decision_date"]),
                        trade_date=current_date,
                        model_version=model_version,
                        symbol=symbol,
                        side="sell",
                        shares=0,
                        price=float(quote["open"]),
                        fee=0,
                        rejected_reason="OPEN_LIMIT_DOWN_OR_SUSPENDED",
                    ),
                )
                continue
            value = sell_shares * float(quote["open"])
            fee = value * fee_rate
            cash += value - fee
            positions[symbol] = shares - sell_shares
            if positions[symbol] == 0:
                positions.pop(symbol)
                position_factors.pop(symbol, None)
            append_execution(
                ledger_path,
                ExecutionEvent(
                    decision_date=date.fromisoformat(pending["decision_date"]),
                    trade_date=current_date,
                    model_version=model_version,
                    symbol=symbol,
                    side="sell" if target_shares == 0 else "reduce",
                    shares=sell_shares,
                    price=float(quote["open"]),
                    fee=fee,
                ),
            )

        for symbol, weight in sorted(targets.items(), key=lambda item: (-item[1], item[0])):
            quote = prices.get(symbol)
            if not quote or weight <= 0:
                continue
            target_shares = int((open_equity * weight / float(quote["open"])) // 100 * 100)
            buy_shares = max(0, target_shares - positions.get(symbol, 0))
            if buy_shares == 0:
                continue
            if not quote["can_buy_open"]:
                append_execution(
                    ledger_path,
                    ExecutionEvent(
                        decision_date=date.fromisoformat(pending["decision_date"]),
                        trade_date=current_date,
                        model_version=model_version,
                        symbol=symbol,
                        side="buy",
                        shares=0,
                        price=float(quote["open"]),
                        fee=0,
                        rejected_reason="OPEN_LIMIT_UP_OR_SUSPENDED",
                    ),
                )
                continue
            affordable = int((cash / (float(quote["open"]) * (1 + fee_rate))) // 100 * 100)
            buy_shares = min(buy_shares, affordable)
            if buy_shares <= 0:
                continue
            value = buy_shares * float(quote["open"])
            fee = value * fee_rate
            cash -= value + fee
            positions[symbol] = positions.get(symbol, 0) + buy_shares
            position_factors[symbol] = float(quote["adj_factor"])
            append_execution(
                ledger_path,
                ExecutionEvent(
                    decision_date=date.fromisoformat(pending["decision_date"]),
                    trade_date=current_date,
                    model_version=model_version,
                    symbol=symbol,
                    side="buy",
                    shares=buy_shares,
                    price=float(quote["open"]),
                    fee=fee,
                ),
            )

    equity = cash + sum(
        shares * float(prices[symbol]["close"])
        for symbol, shares in positions.items()
        if symbol in prices
    )
    state.update(
        {
            "cash": cash,
            "positions": positions,
            "position_factors": position_factors,
            "last_mark_date": current_date.isoformat(),
        }
    )
    state["equity_curve"].append(
        {
            "date": current_date.isoformat(),
            "equity": equity,
            "cash": cash,
            "positions": len(positions),
        }
    )
    _atomic_json(state_path, state)
    return state


def set_pending_targets(
    state_path: Path | str,
    state: dict,
    decision_date: str,
    targets: dict[str, float],
) -> dict:
    state["pending"] = {
        "decision_date": decision_date,
        "targets": targets,
    }
    _atomic_json(Path(state_path), state)
    return state


def current_weights(state: dict, prices: dict[str, float]) -> dict[str, float]:
    values = {
        symbol: int(shares) * prices.get(symbol, 0.0)
        for symbol, shares in state["positions"].items()
    }
    total = float(state["cash"]) + sum(values.values())
    return {symbol: value / total for symbol, value in values.items()} if total > 0 else {}
