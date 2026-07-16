"""Deterministic D-close to D+1-open portfolio simulator for model signals."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from math import sqrt
from statistics import fmean, pstdev


@dataclass(frozen=True)
class PredictionBar:
    decision_date: date
    trade_date: date
    symbol: str
    score: float
    close: float
    next_open: float
    next_close: float
    can_buy: bool = True
    can_sell: bool = True


@dataclass(frozen=True)
class PortfolioConfig:
    start_cash: float = 1_000_000
    max_positions: int = 4
    fee_bps: float = 10
    min_expected_return: float = 0.003
    switch_buffer: float = 0.002
    rebalance_threshold_pct: float = 5


@dataclass(frozen=True)
class SimulatedTrade:
    decision_date: date
    trade_date: date
    symbol: str
    side: str
    amount: float
    fee: float
    reason: str


@dataclass(frozen=True)
class EquityPoint:
    date: date
    equity: float
    cash: float
    positions: int


@dataclass(frozen=True)
class PortfolioResult:
    total_return_pct: float
    sharpe: float
    max_drawdown_pct: float
    cvar_5_pct: float
    turnover_pct: float
    average_hold_bars: float
    closed_trades: int
    equity_curve: tuple[EquityPoint, ...]
    trades: tuple[SimulatedTrade, ...]


def _desired_symbols(
    rows: dict[str, PredictionBar],
    held: set[str],
    config: PortfolioConfig,
) -> list[str]:
    ranked = sorted(
        (row for row in rows.values() if row.score >= config.min_expected_return),
        key=lambda row: (-row.score, row.symbol),
    )
    if not ranked:
        return []
    top = ranked[: config.max_positions]
    cutoff = top[-1].score
    retained = sorted(
        (
            row
            for row in ranked
            if row.symbol in held and row.score >= cutoff - config.switch_buffer
        ),
        key=lambda row: (-row.score, row.symbol),
    )
    desired = [row.symbol for row in retained[: config.max_positions]]
    for row in ranked:
        if len(desired) >= config.max_positions:
            break
        if row.symbol not in desired:
            desired.append(row.symbol)
    return desired


def simulate_portfolio(
    predictions: list[PredictionBar],
    config: PortfolioConfig | None = None,
) -> PortfolioResult:
    config = config or PortfolioConfig()
    if config.start_cash <= 0 or config.max_positions <= 0:
        raise ValueError("start_cash and max_positions must be positive")
    grouped: dict[date, dict[str, PredictionBar]] = {}
    for row in predictions:
        day = grouped.setdefault(row.decision_date, {})
        if row.symbol in day:
            raise ValueError(f"duplicate prediction: {row.decision_date} {row.symbol}")
        if min(row.close, row.next_open, row.next_close) <= 0:
            raise ValueError(f"non-positive price: {row.decision_date} {row.symbol}")
        day[row.symbol] = row

    fee_rate = config.fee_bps / 10_000
    cash = config.start_cash
    positions: dict[str, float] = {}
    entry_bar: dict[str, int] = {}
    hold_lengths: list[int] = []
    trades: list[SimulatedTrade] = []
    curve: list[EquityPoint] = []
    turnover = 0.0

    for bar_index, decision_date in enumerate(sorted(grouped)):
        rows = grouped[decision_date]
        trade_date = min(row.trade_date for row in rows.values())

        for symbol, value in list(positions.items()):
            row = rows.get(symbol)
            if row:
                positions[symbol] = value * row.next_open / row.close

        open_equity = cash + sum(positions.values())
        desired = _desired_symbols(rows, set(positions), config)
        target_value = open_equity / config.max_positions if desired else 0.0
        threshold = open_equity * config.rebalance_threshold_pct / 100

        for symbol, value in sorted(list(positions.items())):
            wanted = target_value if symbol in desired else 0.0
            amount = value - wanted
            if amount <= threshold:
                continue
            row = rows.get(symbol)
            if row is None or not row.can_sell:
                continue
            fee = amount * fee_rate
            positions[symbol] = wanted
            cash += amount - fee
            turnover += amount
            trades.append(
                SimulatedTrade(
                    decision_date,
                    row.trade_date,
                    symbol,
                    "sell" if wanted == 0 else "reduce",
                    amount,
                    fee,
                    "OUTSIDE_TARGET" if wanted == 0 else "TARGET_REBALANCE",
                )
            )
            if wanted == 0:
                positions.pop(symbol)
                hold_lengths.append(bar_index - entry_bar.pop(symbol) + 1)

        for symbol in desired:
            current = positions.get(symbol, 0.0)
            deficit = target_value - current
            if deficit <= threshold:
                continue
            if symbol not in positions and len(positions) >= config.max_positions:
                continue
            row = rows[symbol]
            if not row.can_buy:
                continue
            amount = min(deficit, cash / (1 + fee_rate))
            if amount <= 0:
                continue
            fee = amount * fee_rate
            if symbol not in positions:
                entry_bar[symbol] = bar_index
            positions[symbol] = current + amount
            cash -= amount + fee
            turnover += amount
            trades.append(
                SimulatedTrade(
                    decision_date,
                    row.trade_date,
                    symbol,
                    "buy",
                    amount,
                    fee,
                    "POSITIVE_EXPECTED_UTILITY",
                )
            )

        for symbol, value in list(positions.items()):
            row = rows.get(symbol)
            if row:
                positions[symbol] = value * row.next_close / row.next_open
        equity = cash + sum(positions.values())
        curve.append(EquityPoint(trade_date, equity, cash, len(positions)))

    if not curve:
        return PortfolioResult(0, 0, 0, 0, 0, 0, 0, (), ())

    hold_lengths.extend(len(curve) - entry for entry in entry_bar.values())
    equities = [config.start_cash, *[point.equity for point in curve]]
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
    average_equity = fmean(equities)
    return PortfolioResult(
        total_return_pct=(equities[-1] / equities[0] - 1) * 100,
        sharpe=sharpe,
        max_drawdown_pct=max_drawdown * 100,
        cvar_5_pct=cvar * 100,
        turnover_pct=turnover / average_equity * 100 if average_equity else 0.0,
        average_hold_bars=fmean(hold_lengths) if hold_lengths else 0.0,
        closed_trades=len(hold_lengths) - len(entry_bar),
        equity_curve=tuple(curve),
        trades=tuple(trades),
    )
