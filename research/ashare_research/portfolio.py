"""Deterministic point-in-time A-share portfolio simulator.

Daily model scores are known after decision-day close and execute at the next
tradable open.  The simulator keeps an exact cash/share ledger, applies the
shared production cost model, and enforces A-share lot, concentration,
liquidity, price-limit, and minimum-hold constraints.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from math import sqrt
from statistics import fmean, pstdev

from .cost_config import load_cost_model
from .trading_constraints import load_trading_constraints

_COST_MODEL = load_cost_model()
_CONSTRAINTS = load_trading_constraints()


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
    adjustment_factor: float = 1.0
    next_adjustment_factor: float = 1.0
    liquidity_amount_yuan: float | None = None
    volatility_20: float | None = None
    theme: str | None = None
    ranking_score: float | None = None


@dataclass(frozen=True)
class PortfolioConfig:
    start_cash: float = _CONSTRAINTS.initial_capital_yuan
    max_positions: int = _CONSTRAINTS.max_positions
    # Legacy symmetric fee override used by deterministic unit tests.  When
    # provided it disables the production commission/stamp/slippage model.
    fee_bps: float | None = None
    buy_commission_bps: float = _COST_MODEL.buy_commission_bps
    sell_commission_bps: float = _COST_MODEL.sell_commission_bps
    stamp_duty_bps: float = _COST_MODEL.stamp_duty_bps
    base_slippage_bps: float = _COST_MODEL.base_slippage_bps
    minimum_commission_yuan: float = _COST_MODEL.minimum_commission_yuan
    impact_coefficient: float = _COST_MODEL.impact_coefficient
    max_impact_bps: float = _COST_MODEL.max_impact_bps
    missing_turnover_penalty_bps: float = (
        _COST_MODEL.missing_turnover_penalty_bps
    )
    min_expected_return: float = 0.003
    switch_buffer: float = 0.002
    rebalance_threshold_pct: float = _CONSTRAINTS.rebalance_threshold_pct
    max_single_position_pct: float = _CONSTRAINTS.max_single_position_pct
    max_single_theme_pct: float = _CONSTRAINTS.max_single_theme_pct
    max_order_liquidity_pct: float = _CONSTRAINTS.max_order_bar_amount_pct
    min_holding_bars: int = _CONSTRAINTS.min_holding_bars
    lot_size: int = _CONSTRAINTS.lot_size
    require_liquidity: bool = True
    cost_multiplier: float = 1.0

    def __post_init__(self) -> None:
        positive = {
            "start_cash": self.start_cash,
            "max_positions": self.max_positions,
            "max_single_position_pct": self.max_single_position_pct,
            "max_single_theme_pct": self.max_single_theme_pct,
            "max_order_liquidity_pct": self.max_order_liquidity_pct,
            "lot_size": self.lot_size,
            "cost_multiplier": self.cost_multiplier,
        }
        if any(not math.isfinite(float(value)) or value <= 0 for value in positive.values()):
            raise ValueError("portfolio capital, limits, lot size, and cost multiplier must be positive")
        non_negative = (
            self.buy_commission_bps,
            self.sell_commission_bps,
            self.stamp_duty_bps,
            self.base_slippage_bps,
            self.minimum_commission_yuan,
            self.impact_coefficient,
            self.max_impact_bps,
            self.missing_turnover_penalty_bps,
            self.rebalance_threshold_pct,
            self.min_holding_bars,
        )
        if any(not math.isfinite(float(value)) or value < 0 for value in non_negative):
            raise ValueError("portfolio fees and thresholds must be non-negative")
        if self.fee_bps is not None and (
            not math.isfinite(self.fee_bps) or self.fee_bps < 0
        ):
            raise ValueError("fee_bps must be non-negative")
        if self.max_positions > _CONSTRAINTS.max_positions:
            raise ValueError("max_positions exceeds the shared production constraint")
        if self.max_single_position_pct > _CONSTRAINTS.max_single_position_pct:
            raise ValueError("single-position limit exceeds the shared production constraint")
        if self.max_single_theme_pct > _CONSTRAINTS.max_single_theme_pct:
            raise ValueError("theme limit exceeds the shared production constraint")


@dataclass(frozen=True)
class SimulatedTrade:
    decision_date: date
    trade_date: date
    symbol: str
    side: str
    amount: float
    fee: float
    reason: str
    shares: float = 0
    price: float = 0.0
    effective_price: float = 0.0
    commission: float = 0.0
    stamp_duty: float = 0.0
    slippage: float = 0.0
    impact_bps: float = 0.0
    net_cash_flow: float = 0.0
    execution_time: str = ""


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


@dataclass
class _Position:
    shares: float
    mark_price: float
    adjustment_factor: float
    entry_bar: int
    theme: str


@dataclass(frozen=True)
class _ExecutionCost:
    gross_notional: float
    commission: float
    stamp_duty: float
    slippage: float
    impact_bps: float
    cash_change: float
    effective_price: float


def _theme_key(row: PredictionBar) -> str:
    # Missing metadata must not collapse unrelated stocks into one artificial
    # theme bucket.  Production evaluation supplies the curated theme.
    return row.theme or f"UNCLASSIFIED:{row.symbol}"


def _ranking_score(row: PredictionBar) -> float:
    return row.score if row.ranking_score is None else row.ranking_score


def _desired_symbols(
    rows: dict[str, PredictionBar],
    held: set[str],
    config: PortfolioConfig,
) -> list[str]:
    ranked = sorted(
        (row for row in rows.values() if row.score >= config.min_expected_return),
        key=lambda row: (-_ranking_score(row), row.symbol),
    )
    if not ranked:
        return []
    top = ranked[: config.max_positions]
    cutoff = _ranking_score(top[-1])
    retained = sorted(
        (
            row
            for row in ranked
            if row.symbol in held
            and _ranking_score(row) >= cutoff - config.switch_buffer
        ),
        key=lambda row: (-_ranking_score(row), row.symbol),
    )
    desired = [row.symbol for row in retained[: config.max_positions]]
    for row in ranked:
        if len(desired) >= config.max_positions:
            break
        if row.symbol not in desired:
            desired.append(row.symbol)
    return desired


def _target_values(
    desired: list[str],
    rows: dict[str, PredictionBar],
    equity: float,
    config: PortfolioConfig,
) -> dict[str, float]:
    if not desired:
        return {}
    slot_target = min(
        equity / config.max_positions,
        equity * config.max_single_position_pct / 100.0,
    )
    theme_cap = equity * config.max_single_theme_pct / 100.0
    theme_allocated: dict[str, float] = {}
    targets: dict[str, float] = {}
    for symbol in desired:
        theme = _theme_key(rows[symbol])
        remaining = max(0.0, theme_cap - theme_allocated.get(theme, 0.0))
        target = min(slot_target, remaining)
        if target <= 0:
            continue
        targets[symbol] = target
        theme_allocated[theme] = theme_allocated.get(theme, 0.0) + target
    return targets


def _impact_bps(
    notional: float,
    row: PredictionBar,
    config: PortfolioConfig,
) -> float:
    if config.fee_bps is not None:
        return 0.0
    liquidity = row.liquidity_amount_yuan
    if liquidity is None or not math.isfinite(liquidity) or liquidity <= 0:
        return config.missing_turnover_penalty_bps * config.cost_multiplier
    volatility = row.volatility_20
    if volatility is None or not math.isfinite(volatility) or volatility <= 0:
        volatility = 0.02
    participation = max(0.0, notional / liquidity)
    impact = config.impact_coefficient * volatility * sqrt(participation) * 10_000
    return min(config.max_impact_bps, impact) * config.cost_multiplier


def _execution_cost(
    side: str,
    price: float,
    shares: float,
    row: PredictionBar,
    config: PortfolioConfig,
) -> _ExecutionCost:
    gross = price * shares
    if config.fee_bps is not None:
        commission = gross * config.fee_bps / 10_000
        stamp = 0.0
        slippage = 0.0
        impact = 0.0
    else:
        commission_bps = (
            config.buy_commission_bps
            if side == "buy"
            else config.sell_commission_bps
        )
        commission = max(
            gross * commission_bps * config.cost_multiplier / 10_000,
            config.minimum_commission_yuan * config.cost_multiplier,
        )
        stamp = (
            gross * config.stamp_duty_bps * config.cost_multiplier / 10_000
            if side == "sell"
            else 0.0
        )
        impact = _impact_bps(gross, row, config)
        slippage_bps = (
            config.base_slippage_bps * config.cost_multiplier + impact
        )
        slippage = gross * slippage_bps / 10_000
    total_cost = commission + stamp + slippage
    cash_change = -(gross + total_cost) if side == "buy" else gross - total_cost
    effective_price = (
        (gross + total_cost) / shares
        if side == "buy"
        else (gross - total_cost) / shares
    )
    return _ExecutionCost(
        gross_notional=gross,
        commission=commission,
        stamp_duty=stamp,
        slippage=slippage,
        impact_bps=impact,
        cash_change=cash_change,
        effective_price=effective_price,
    )


def _capacity_notional(row: PredictionBar, config: PortfolioConfig) -> float:
    liquidity = row.liquidity_amount_yuan
    if liquidity is None or not math.isfinite(liquidity) or liquidity <= 0:
        return 0.0 if config.require_liquidity else math.inf
    return liquidity * config.max_order_liquidity_pct / 100.0


def _round_down_shares(value: float, price: float, lot_size: int) -> int:
    if value <= 0 or price <= 0:
        return 0
    return int(value / price) // lot_size * lot_size


def _trade_record(
    row: PredictionBar,
    side: str,
    reason: str,
    shares: float,
    cost: _ExecutionCost,
) -> SimulatedTrade:
    total_fee = cost.commission + cost.stamp_duty + cost.slippage
    return SimulatedTrade(
        decision_date=row.decision_date,
        trade_date=row.trade_date,
        symbol=row.symbol,
        side=side,
        amount=cost.gross_notional,
        fee=total_fee,
        reason=reason,
        shares=shares,
        price=row.next_open,
        effective_price=cost.effective_price,
        commission=cost.commission,
        stamp_duty=cost.stamp_duty,
        slippage=cost.slippage,
        impact_bps=cost.impact_bps,
        net_cash_flow=cost.cash_change,
    )


def simulate_portfolio(
    predictions: list[PredictionBar],
    config: PortfolioConfig | None = None,
) -> PortfolioResult:
    config = config or PortfolioConfig()
    grouped: dict[date, dict[str, PredictionBar]] = {}
    for row in predictions:
        day = grouped.setdefault(row.decision_date, {})
        if row.symbol in day:
            raise ValueError(f"duplicate prediction: {row.decision_date} {row.symbol}")
        numeric = (
            row.score,
            row.close,
            row.next_open,
            row.next_close,
            row.adjustment_factor,
            row.next_adjustment_factor,
        )
        if any(not math.isfinite(value) for value in numeric):
            raise ValueError(f"non-finite prediction bar: {row.decision_date} {row.symbol}")
        if row.ranking_score is not None and not math.isfinite(
            row.ranking_score
        ):
            raise ValueError(
                f"non-finite ranking score: {row.decision_date} {row.symbol}"
            )
        if min(numeric[1:]) <= 0:
            raise ValueError(f"non-positive price: {row.decision_date} {row.symbol}")
        day[row.symbol] = row

    cash = config.start_cash
    positions: dict[str, _Position] = {}
    hold_lengths: list[int] = []
    closed_trades = 0
    trades: list[SimulatedTrade] = []
    curve: list[EquityPoint] = []
    turnover = 0.0

    for bar_index, decision_date in enumerate(sorted(grouped)):
        rows = grouped[decision_date]
        trade_dates = {row.trade_date for row in rows.values()}
        if len(trade_dates) != 1:
            raise ValueError(f"inconsistent trade_date for decision date {decision_date}")
        trade_date = next(iter(trade_dates))

        for symbol, position in positions.items():
            row = rows.get(symbol)
            if row is None:
                continue
            adjustment_ratio = (
                row.next_adjustment_factor / position.adjustment_factor
            )
            if not math.isfinite(adjustment_ratio) or adjustment_ratio <= 0:
                raise ValueError(
                    f"invalid adjustment ratio: {decision_date} {symbol}"
                )
            position.shares *= adjustment_ratio
            position.adjustment_factor = row.next_adjustment_factor

        open_prices = {
            symbol: rows[symbol].next_open
            if symbol in rows
            else position.mark_price
            for symbol, position in positions.items()
        }
        open_equity = cash + sum(
            position.shares * open_prices[symbol]
            for symbol, position in positions.items()
        )
        desired = _desired_symbols(rows, set(positions), config)
        targets = _target_values(desired, rows, open_equity, config)
        threshold = open_equity * config.rebalance_threshold_pct / 100

        for symbol in sorted(list(positions)):
            position = positions[symbol]
            row = rows.get(symbol)
            current_value = position.shares * open_prices[symbol]
            wanted = targets.get(symbol, 0.0)
            deficit = current_value - wanted
            if deficit <= threshold or row is None or not row.can_sell:
                continue
            held_bars = bar_index - position.entry_bar
            if held_bars < config.min_holding_bars:
                continue
            capacity = _capacity_notional(row, config)
            sell_value = min(deficit, capacity)
            shares = _round_down_shares(sell_value, row.next_open, config.lot_size)
            if wanted == 0 and sell_value >= current_value - row.next_open:
                shares = position.shares
            shares = min(float(shares), position.shares)
            if shares <= 0:
                continue
            cost = _execution_cost("sell", row.next_open, shares, row, config)
            cash += cost.cash_change
            position.shares -= shares
            turnover += cost.gross_notional
            side = "sell" if position.shares == 0 else "reduce"
            reason = "OUTSIDE_TARGET" if wanted == 0 else "TARGET_REBALANCE"
            trades.append(_trade_record(row, side, reason, shares, cost))
            if position.shares <= 1e-9:
                position.shares = 0
                hold_lengths.append(held_bars + 1)
                positions.pop(symbol)
                closed_trades += 1

        for symbol in desired:
            row = rows[symbol]
            target = targets.get(symbol, 0.0)
            position = positions.get(symbol)
            current_value = (
                position.shares * row.next_open if position is not None else 0.0
            )
            deficit = target - current_value
            if deficit <= threshold or not row.can_buy:
                continue
            if position is None and len(positions) >= config.max_positions:
                continue
            capacity = _capacity_notional(row, config)
            buy_value = min(deficit, capacity)
            shares = _round_down_shares(buy_value, row.next_open, config.lot_size)
            while shares > 0:
                cost = _execution_cost("buy", row.next_open, shares, row, config)
                if -cost.cash_change <= cash + 1e-9:
                    break
                shares -= config.lot_size
            if shares <= 0:
                continue
            cost = _execution_cost("buy", row.next_open, shares, row, config)
            cash += cost.cash_change
            turnover += cost.gross_notional
            if position is None:
                positions[symbol] = _Position(
                    shares=shares,
                    mark_price=row.next_open,
                    adjustment_factor=row.next_adjustment_factor,
                    entry_bar=bar_index,
                    theme=_theme_key(row),
                )
            else:
                position.shares += shares
            trades.append(
                _trade_record(
                    row,
                    "buy",
                    "POSITIVE_EXPECTED_UTILITY",
                    shares,
                    cost,
                )
            )

        for symbol, position in positions.items():
            row = rows.get(symbol)
            if row is not None:
                position.mark_price = row.next_close
        equity = cash + sum(
            position.shares * position.mark_price for position in positions.values()
        )
        curve.append(EquityPoint(trade_date, equity, cash, len(positions)))

    if not curve:
        return PortfolioResult(0, 0, 0, 0, 0, 0, 0, (), ())

    hold_lengths.extend(
        len(curve) - position.entry_bar for position in positions.values()
    )
    equities = [config.start_cash, *[point.equity for point in curve]]
    returns = [
        equities[index] / equities[index - 1] - 1
        for index in range(1, len(equities))
    ]
    volatility = pstdev(returns) if len(returns) > 1 else 0.0
    sharpe = fmean(returns) / volatility * sqrt(252) if volatility > 0 else 0.0
    peak = equities[0]
    max_drawdown = 0.0
    for equity in equities:
        peak = max(peak, equity)
        max_drawdown = min(max_drawdown, equity / peak - 1)
    tail_count = max(1, math.ceil(len(returns) * 0.05))
    cvar = fmean(sorted(returns)[:tail_count]) if returns else 0.0
    average_equity = fmean(equities)
    return PortfolioResult(
        total_return_pct=(equities[-1] / equities[0] - 1) * 100,
        sharpe=sharpe,
        max_drawdown_pct=max_drawdown * 100,
        cvar_5_pct=cvar * 100,
        turnover_pct=turnover / average_equity * 100 if average_equity else 0.0,
        average_hold_bars=fmean(hold_lengths) if hold_lengths else 0.0,
        closed_trades=closed_trades,
        equity_curve=tuple(curve),
        trades=tuple(trades),
    )
