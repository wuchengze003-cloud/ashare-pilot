"""Event-ordered 5-minute execution layer for the production strategy race.

Daily cross-sectional scores are observable after D close.  Candidate-specific
entry confirmation is evaluated on D+1 5-minute bars and fills at the next
bar's open.  Existing positions can trigger a hard or trailing stop, but only
settled shares can be sold.  The engine keeps one cash/share ledger and uses
the shared production cost and risk contracts.
"""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from math import sqrt
from pathlib import Path
from statistics import fmean, pstdev
from typing import Any

import polars as pl

from .minute_data import _symbol_to_ts_code
from .portfolio import (
    EquityPoint,
    PortfolioConfig,
    PortfolioResult,
    PredictionBar,
    SimulatedTrade,
    _desired_symbols,
    _execution_cost,
    _target_values,
    _theme_key,
)

EXPECTED_5MIN_BARS = 48
MinuteLoader = Callable[[str, date], pl.DataFrame]


@dataclass(frozen=True)
class MinuteOverlayParams:
    confirmation_bars: int
    vwap_buffer_bps: float
    maximum_chase_pct: float
    hard_stop_pct: float
    trailing_stop_pct: float
    minimum_volume_ratio: float
    rebound_from_low_pct: float

    @classmethod
    def from_mapping(cls, values: dict[str, float]) -> MinuteOverlayParams:
        required = {
            "confirmation_bars",
            "vwap_buffer_bps",
            "maximum_chase_pct",
            "hard_stop_pct",
            "trailing_stop_pct",
            "minimum_volume_ratio",
            "rebound_from_low_pct",
        }
        missing = required - set(values)
        if missing:
            raise ValueError(
                f"minute overlay parameters missing: {sorted(missing)}"
            )
        confirmation = values["confirmation_bars"]
        if not float(confirmation).is_integer() or confirmation < 1:
            raise ValueError("confirmation_bars must be a positive integer")
        result = cls(
            confirmation_bars=int(confirmation),
            vwap_buffer_bps=float(values["vwap_buffer_bps"]),
            maximum_chase_pct=float(values["maximum_chase_pct"]),
            hard_stop_pct=float(values["hard_stop_pct"]),
            trailing_stop_pct=float(values["trailing_stop_pct"]),
            minimum_volume_ratio=float(values["minimum_volume_ratio"]),
            rebound_from_low_pct=float(values["rebound_from_low_pct"]),
        )
        finite = (
            result.vwap_buffer_bps,
            result.maximum_chase_pct,
            result.hard_stop_pct,
            result.trailing_stop_pct,
            result.minimum_volume_ratio,
            result.rebound_from_low_pct,
        )
        if any(not math.isfinite(value) for value in finite):
            raise ValueError("minute overlay parameters must be finite")
        if not 0 < result.maximum_chase_pct <= 0.10:
            raise ValueError("maximum_chase_pct must be in (0, 0.10]")
        if not 0 < result.hard_stop_pct <= 0.20:
            raise ValueError("hard_stop_pct must be in (0, 0.20]")
        if not 0 < result.trailing_stop_pct <= 0.25:
            raise ValueError("trailing_stop_pct must be in (0, 0.25]")
        if result.minimum_volume_ratio < 0:
            raise ValueError("minimum_volume_ratio must be non-negative")
        if not 0 <= result.rebound_from_low_pct <= 0.10:
            raise ValueError("rebound_from_low_pct must be in [0, 0.10]")
        return result


@dataclass(frozen=True)
class MinuteRequirement:
    symbol: str
    trade_date: date


class MissingMinuteDataError(ValueError):
    def __init__(self, requirements: Iterable[MinuteRequirement]) -> None:
        unique = tuple(
            sorted(
                set(requirements),
                key=lambda item: (item.trade_date, item.symbol),
            )
        )
        self.requirements = unique
        preview = ", ".join(
            f"{item.symbol}@{item.trade_date}" for item in unique[:5]
        )
        suffix = "" if len(unique) <= 5 else f" and {len(unique) - 5} more"
        super().__init__(f"minute data missing or invalid: {preview}{suffix}")


@dataclass
class _Position:
    shares: float
    unsettled_shares: float
    mark_price: float
    adjustment_factor: float
    entry_bar: int
    entry_adjusted_price: float
    peak_adjusted_price: float
    theme: str
    entry_time: str

    @property
    def settled_shares(self) -> float:
        return max(0.0, self.shares - self.unsettled_shares)


@dataclass(frozen=True)
class _Event:
    time: str
    side: str
    symbol: str
    price: float
    bar_amount: float
    reason: str
    target_value: float
    full_exit: bool = False


class ParquetMinuteStore:
    """On-demand month cache over the partitioned minute warehouse."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self._cache: dict[tuple[str, int, int], pl.DataFrame] = {}

    def __call__(self, symbol: str, trade_date: date) -> pl.DataFrame:
        ts_code = _symbol_to_ts_code(symbol)
        key = (ts_code, trade_date.year, trade_date.month)
        cached = self._cache.get(key)
        if cached is None:
            target = (
                self.root
                / "raw"
                / "freq=5min"
                / f"ts_code={ts_code}"
                / f"year={trade_date.year}"
                / f"month={trade_date.month:02d}"
                / "part.parquet"
            )
            if target.is_file() and target.stat().st_size > 0:
                cached = (
                    pl.read_parquet(target)
                    .unique(
                        subset=["ts_code", "trade_time", "freq"],
                        keep="last",
                    )
                    .sort("trade_time")
                )
            else:
                cached = pl.DataFrame()
            self._cache[key] = cached
        if cached.is_empty() or "trade_date" not in cached.columns:
            return pl.DataFrame()
        compact = trade_date.strftime("%Y%m%d")
        return cached.filter(
            pl.col("trade_date").cast(pl.String) == compact
        )


def prediction_bars_from_artifact(frame: pl.DataFrame) -> list[PredictionBar]:
    required = {
        "date",
        "symbol",
        "raw_score",
        "prediction",
        "close",
        "next_trade_date",
        "next_raw_open",
        "next_raw_close",
        "adj_factor",
        "next_adj_factor",
        "next_up_limit",
        "next_down_limit",
        "next_is_suspended",
        "amount",
        "volatility_20",
        "theme",
    }
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(
            f"minute candidate artifact missing columns: {sorted(missing)}"
        )
    complete = frame.drop_nulls(
        [
            "prediction",
            "close",
            "next_trade_date",
            "next_raw_open",
            "next_raw_close",
            "adj_factor",
            "next_adj_factor",
        ]
    )
    return [
        PredictionBar(
            decision_date=row["date"],
            trade_date=row["next_trade_date"],
            symbol=str(row["symbol"]),
            score=float(row["prediction"]),
            ranking_score=(
                float(row["raw_score"])
                if row["raw_score"] is not None
                else float(row["prediction"])
            ),
            close=float(row["close"]),
            next_open=float(row["next_raw_open"]),
            next_close=float(row["next_raw_close"]),
            adjustment_factor=float(row["adj_factor"]),
            next_adjustment_factor=float(row["next_adj_factor"]),
            can_buy=not bool(row["next_is_suspended"]),
            can_sell=not bool(row["next_is_suspended"]),
            liquidity_amount_yuan=(
                float(row["amount"]) if row["amount"] is not None else None
            ),
            volatility_20=(
                float(row["volatility_20"])
                if row["volatility_20"] is not None
                else None
            ),
            theme=str(row["theme"]),
        )
        for row in complete.iter_rows(named=True)
    ]


def _limits_from_artifact(
    frame: pl.DataFrame,
) -> dict[tuple[date, str], tuple[float, float, bool]]:
    rows = frame.select(
        "date",
        "symbol",
        "next_up_limit",
        "next_down_limit",
        "next_is_suspended",
    )
    result: dict[tuple[date, str], tuple[float, float, bool]] = {}
    for row in rows.iter_rows(named=True):
        if row["next_up_limit"] is None or row["next_down_limit"] is None:
            continue
        result[(row["date"], str(row["symbol"]))] = (
            float(row["next_up_limit"]),
            float(row["next_down_limit"]),
            bool(row["next_is_suspended"]),
        )
    return result


def collect_minute_requirements(
    predictions: list[PredictionBar],
    config: PortfolioConfig | None = None,
) -> tuple[MinuteRequirement, ...]:
    """Conservatively collect symbol-days needed by all possible fills.

    The rolling tail covers ordinary minimum-hold exits without downloading
    every CSI800 constituent for every date.  A later simulation still fails
    closed if a prolonged limit-down or suspension needs an additional day.
    """
    config = config or PortfolioConfig()
    grouped = _group_predictions(predictions)
    recent: deque[set[str]] = deque(
        maxlen=max(config.min_holding_bars + 2, 3)
    )
    requirements: set[MinuteRequirement] = set()
    for decision_date in sorted(grouped):
        rows = grouped[decision_date]
        trade_date = _single_trade_date(rows, decision_date)
        desired = set(_desired_symbols(rows, set().union(*recent), config))
        relevant = desired | set().union(*recent)
        for symbol in relevant:
            row = rows.get(symbol)
            if row is not None and row.can_buy:
                requirements.add(MinuteRequirement(symbol, trade_date))
        recent.append(desired)
    return tuple(
        sorted(
            requirements,
            key=lambda item: (item.trade_date, item.symbol),
        )
    )


def requirement_ranges(
    requirements: Iterable[MinuteRequirement],
) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[date]] = {}
    for item in requirements:
        grouped.setdefault(item.symbol, []).append(item.trade_date)
    return {
        symbol: {
            "start": str(min(dates)),
            "end": str(max(dates)),
            "dates": [str(value) for value in sorted(set(dates))],
        }
        for symbol, dates in sorted(grouped.items())
    }


def _group_predictions(
    predictions: list[PredictionBar],
) -> dict[date, dict[str, PredictionBar]]:
    grouped: dict[date, dict[str, PredictionBar]] = {}
    for row in predictions:
        day = grouped.setdefault(row.decision_date, {})
        if row.symbol in day:
            raise ValueError(
                f"duplicate minute prediction: {row.decision_date} {row.symbol}"
            )
        day[row.symbol] = row
    return grouped


def _single_trade_date(
    rows: dict[str, PredictionBar],
    decision_date: date,
) -> date:
    values = {row.trade_date for row in rows.values()}
    if len(values) != 1:
        raise ValueError(
            f"inconsistent minute trade date for {decision_date}: {values}"
        )
    return next(iter(values))


def _validated_bars(
    loader: MinuteLoader,
    symbol: str,
    trade_date: date,
) -> pl.DataFrame:
    frame = loader(symbol, trade_date)
    required = {
        "trade_date",
        "trade_time",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "amount",
    }
    if frame.is_empty() or not required <= set(frame.columns):
        raise MissingMinuteDataError([MinuteRequirement(symbol, trade_date)])
    compact = trade_date.strftime("%Y%m%d")
    selected = (
        frame.filter(pl.col("trade_date").cast(pl.String) == compact)
        .select(*sorted(required))
        .unique("trade_time", keep="last")
        .sort("trade_time")
    )
    if selected.height != EXPECTED_5MIN_BARS:
        raise MissingMinuteDataError([MinuteRequirement(symbol, trade_date)])
    numeric = ("open", "high", "low", "close", "volume", "amount")
    invalid = selected.filter(
        pl.any_horizontal(
            [
                pl.col(column).cast(pl.Float64, strict=False).is_null()
                | ~pl.col(column).cast(pl.Float64, strict=False).is_finite()
                | (
                    pl.col(column).cast(pl.Float64, strict=False) <= 0
                    if column in {"open", "high", "low", "close"}
                    else pl.lit(False)
                )
                for column in numeric
            ]
        )
    )
    if not invalid.is_empty():
        raise MissingMinuteDataError([MinuteRequirement(symbol, trade_date)])
    return selected


def _execution_time(frame: pl.DataFrame, index: int) -> str:
    label = str(frame.row(index, named=True)["trade_time"])
    parsed = datetime.strptime(label, "%Y-%m-%d %H:%M:%S")
    return (parsed - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")


def _vwap(amount: float, volume: float) -> float:
    return amount / volume if volume > 0 else float("nan")


def _entry_event(
    candidate_id: str,
    symbol: str,
    bars: pl.DataFrame,
    row: PredictionBar,
    params: MinuteOverlayParams,
    up_limit: float,
    target_value: float,
) -> _Event | None:
    cumulative_amount = 0.0
    cumulative_volume = 0.0
    cumulative_low = math.inf
    previous_amounts: list[float] = []
    for index in range(bars.height - 1):
        bar = bars.row(index, named=True)
        close = float(bar["close"])
        low = float(bar["low"])
        amount = float(bar["amount"])
        volume = float(bar["volume"])
        cumulative_amount += amount
        cumulative_volume += volume
        cumulative_low = min(cumulative_low, low)
        observed_vwap = _vwap(cumulative_amount, cumulative_volume)
        volume_ratio = (
            amount / fmean(previous_amounts)
            if previous_amounts and fmean(previous_amounts) > 0
            else 0.0
        )
        previous_amounts.append(amount)
        if index + 1 < params.confirmation_bars:
            continue
        above_vwap = (
            math.isfinite(observed_vwap)
            and close
            >= observed_vwap * (1 + params.vwap_buffer_bps / 10_000)
        )
        if candidate_id == "anchor-v1":
            confirmed = (
                above_vwap
                and close >= float(bars.row(0, named=True)["open"])
            )
        elif candidate_id == "tide-v3":
            confirmed = (
                above_vwap
                and volume_ratio >= params.minimum_volume_ratio
                and close > float(bars.row(0, named=True)["open"])
            )
        elif candidate_id == "prism-v3":
            rebound = close / cumulative_low - 1 if cumulative_low > 0 else 0.0
            confirmed = above_vwap and rebound >= params.rebound_from_low_pct
        else:
            raise ValueError(f"unsupported minute candidate: {candidate_id}")
        if not confirmed:
            continue
        execution = bars.row(index + 1, named=True)
        price = float(execution["open"])
        execution_vwap_gap = (
            price / observed_vwap - 1 if observed_vwap > 0 else math.inf
        )
        prior_close_gap = price / row.close - 1
        if (
            price >= up_limit - 1e-8
            or execution_vwap_gap > params.maximum_chase_pct
            or prior_close_gap > params.maximum_chase_pct
        ):
            continue
        return _Event(
            time=_execution_time(bars, index + 1),
            side="buy",
            symbol=symbol,
            price=price,
            bar_amount=float(execution["amount"]),
            reason=f"{candidate_id.upper()}_5MIN_CONFIRM",
            target_value=target_value,
        )
    return None


def _first_sell_events(
    symbol: str,
    bars: pl.DataFrame,
    down_limit: float,
    reason: str,
    target_value: float,
    full_exit: bool,
) -> list[_Event]:
    events: list[_Event] = []
    for index in range(bars.height):
        bar = bars.row(index, named=True)
        price = float(bar["open"])
        if price <= down_limit + 1e-8 or float(bar["amount"]) <= 0:
            continue
        events.append(
            _Event(
                time=_execution_time(bars, index),
                side="sell",
                symbol=symbol,
                price=price,
                bar_amount=float(bar["amount"]),
                reason=reason,
                target_value=target_value,
                full_exit=full_exit,
            )
        )
        if not full_exit:
            break
    return events


def _stop_events(
    symbol: str,
    bars: pl.DataFrame,
    position: _Position,
    factor: float,
    params: MinuteOverlayParams,
    down_limit: float,
) -> list[_Event]:
    peak = position.peak_adjusted_price
    for signal_index in range(bars.height - 1):
        bar = bars.row(signal_index, named=True)
        peak = max(peak, float(bar["high"]) * factor)
        adjusted_close = float(bar["close"]) * factor
        hard_stop = adjusted_close <= (
            position.entry_adjusted_price * (1 - params.hard_stop_pct)
        )
        trailing_stop = adjusted_close <= (
            peak * (1 - params.trailing_stop_pct)
        )
        if not hard_stop and not trailing_stop:
            continue
        reason = "HARD_STOP_5MIN" if hard_stop else "TRAILING_STOP_5MIN"
        events: list[_Event] = []
        for execution_index in range(signal_index + 1, bars.height):
            execution = bars.row(execution_index, named=True)
            price = float(execution["open"])
            if price <= down_limit + 1e-8:
                continue
            events.append(
                _Event(
                    time=_execution_time(bars, execution_index),
                    side="sell",
                    symbol=symbol,
                    price=price,
                    bar_amount=float(execution["amount"]),
                    reason=reason,
                    target_value=0.0,
                    full_exit=True,
                )
            )
        return events
    return []


def _round_down_shares(value: float, price: float, lot_size: int) -> int:
    if value <= 0 or price <= 0:
        return 0
    return int(value / price) // lot_size * lot_size


def _trade(
    row: PredictionBar,
    event: _Event,
    shares: float,
    cost: Any,
) -> SimulatedTrade:
    return SimulatedTrade(
        decision_date=row.decision_date,
        trade_date=row.trade_date,
        symbol=row.symbol,
        side=event.side,
        amount=cost.gross_notional,
        fee=cost.commission + cost.stamp_duty + cost.slippage,
        reason=event.reason,
        shares=shares,
        price=event.price,
        effective_price=cost.effective_price,
        commission=cost.commission,
        stamp_duty=cost.stamp_duty,
        slippage=cost.slippage,
        impact_bps=cost.impact_bps,
        net_cash_flow=cost.cash_change,
        execution_time=event.time,
    )


def _summarize(
    config: PortfolioConfig,
    curve: list[EquityPoint],
    trades: list[SimulatedTrade],
    turnover: float,
    hold_lengths: list[int],
    positions: dict[str, _Position],
    closed_trades: int,
) -> PortfolioResult:
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


def simulate_minute_portfolio(
    artifact: pl.DataFrame,
    candidate_id: str,
    params: MinuteOverlayParams | dict[str, float],
    loader: MinuteLoader,
    config: PortfolioConfig | None = None,
) -> PortfolioResult:
    """Run one candidate with daily targets and minute-confirmed executions."""
    config = config or PortfolioConfig()
    overlay = (
        params
        if isinstance(params, MinuteOverlayParams)
        else MinuteOverlayParams.from_mapping(params)
    )
    predictions = prediction_bars_from_artifact(artifact)
    limits = _limits_from_artifact(artifact)
    grouped = _group_predictions(predictions)
    cash = config.start_cash
    positions: dict[str, _Position] = {}
    trades: list[SimulatedTrade] = []
    curve: list[EquityPoint] = []
    turnover = 0.0
    hold_lengths: list[int] = []
    closed_trades = 0
    missing: set[MinuteRequirement] = set()

    for bar_index, decision_date in enumerate(sorted(grouped)):
        rows = grouped[decision_date]
        trade_date = _single_trade_date(rows, decision_date)
        for position in positions.values():
            position.unsettled_shares = 0.0

        for symbol, position in positions.items():
            row = rows.get(symbol)
            if row is None:
                continue
            ratio = row.next_adjustment_factor / position.adjustment_factor
            if not math.isfinite(ratio) or ratio <= 0:
                raise ValueError(
                    f"invalid minute adjustment ratio: {decision_date} {symbol}"
                )
            position.shares *= ratio
            position.unsettled_shares *= ratio
            position.adjustment_factor = row.next_adjustment_factor

        open_prices = {
            symbol: (
                rows[symbol].next_open
                if symbol in rows
                else position.mark_price
            )
            for symbol, position in positions.items()
        }
        open_equity = cash + sum(
            position.shares * open_prices[symbol]
            for symbol, position in positions.items()
        )
        desired = _desired_symbols(rows, set(positions), config)
        targets = _target_values(desired, rows, open_equity, config)
        threshold = open_equity * config.rebalance_threshold_pct / 100
        relevant = set(desired) | set(positions)
        day_bars: dict[str, pl.DataFrame] = {}
        for symbol in sorted(relevant):
            row = rows.get(symbol)
            if row is None:
                continue
            limit = limits.get((decision_date, symbol))
            if limit is None:
                raise ValueError(
                    f"minute limit prices missing: {decision_date} {symbol}"
                )
            if limit[2]:
                continue
            try:
                day_bars[symbol] = _validated_bars(
                    loader,
                    symbol,
                    trade_date,
                )
            except MissingMinuteDataError as error:
                missing.update(error.requirements)
        if missing:
            continue

        events: list[_Event] = []
        for symbol, position in positions.items():
            row = rows.get(symbol)
            bars = day_bars.get(symbol)
            if row is None or bars is None or position.settled_shares <= 0:
                continue
            _, down_limit, _ = limits[(decision_date, symbol)]
            current_value = position.shares * row.next_open
            wanted = targets.get(symbol, 0.0)
            deficit = current_value - wanted
            held_bars = bar_index - position.entry_bar
            if deficit > threshold and held_bars >= config.min_holding_bars:
                events.extend(
                    _first_sell_events(
                        symbol,
                        bars,
                        down_limit,
                        (
                            "OUTSIDE_TARGET"
                            if wanted == 0
                            else "TARGET_REBALANCE"
                        ),
                        wanted,
                        wanted == 0,
                    )
                )
            elif held_bars >= 1:
                events.extend(
                    _stop_events(
                        symbol,
                        bars,
                        position,
                        row.next_adjustment_factor,
                        overlay,
                        down_limit,
                    )
                )

        for symbol in desired:
            row = rows[symbol]
            bars = day_bars.get(symbol)
            if bars is None:
                continue
            up_limit, _, _ = limits[(decision_date, symbol)]
            target = targets.get(symbol, 0.0)
            position = positions.get(symbol)
            current_value = (
                position.shares * row.next_open if position is not None else 0.0
            )
            if target - current_value <= threshold:
                continue
            event = _entry_event(
                candidate_id,
                symbol,
                bars,
                row,
                overlay,
                up_limit,
                target,
            )
            if event is not None:
                events.append(event)

        events.sort(
            key=lambda event: (
                event.time,
                0 if event.side == "sell" else 1,
                event.symbol,
            )
        )
        for event in events:
            row = rows.get(event.symbol)
            if row is None:
                continue
            position = positions.get(event.symbol)
            execution_row = replace(
                row,
                next_open=event.price,
                liquidity_amount_yuan=event.bar_amount,
            )
            capacity = (
                event.bar_amount * config.max_order_liquidity_pct / 100
            )
            if event.side == "sell":
                if position is None or position.settled_shares <= 0:
                    continue
                current_value = position.shares * event.price
                required_value = (
                    current_value
                    if event.full_exit
                    else max(0.0, current_value - event.target_value)
                )
                sell_value = min(required_value, capacity)
                if event.full_exit and capacity >= current_value - event.price:
                    shares = position.settled_shares
                else:
                    shares = min(
                        float(
                            _round_down_shares(
                                sell_value,
                                event.price,
                                config.lot_size,
                            )
                        ),
                        position.settled_shares,
                    )
                if shares <= 0:
                    continue
                cost = _execution_cost(
                    "sell",
                    event.price,
                    shares,
                    execution_row,
                    config,
                )
                cash += cost.cash_change
                position.shares -= shares
                turnover += cost.gross_notional
                trades.append(_trade(row, event, shares, cost))
                if position.shares <= 1e-9:
                    hold_lengths.append(bar_index - position.entry_bar + 1)
                    positions.pop(event.symbol)
                    closed_trades += 1
            else:
                current_value = (
                    position.shares * event.price
                    if position is not None
                    else 0.0
                )
                deficit = event.target_value - current_value
                if deficit <= threshold:
                    continue
                buy_value = min(deficit, capacity)
                shares = _round_down_shares(
                    buy_value,
                    event.price,
                    config.lot_size,
                )
                while shares > 0:
                    cost = _execution_cost(
                        "buy",
                        event.price,
                        shares,
                        execution_row,
                        config,
                    )
                    if -cost.cash_change <= cash + 1e-9:
                        break
                    shares -= config.lot_size
                if shares <= 0:
                    continue
                cost = _execution_cost(
                    "buy",
                    event.price,
                    shares,
                    execution_row,
                    config,
                )
                cash += cost.cash_change
                turnover += cost.gross_notional
                if position is None:
                    positions[event.symbol] = _Position(
                        shares=float(shares),
                        unsettled_shares=float(shares),
                        mark_price=event.price,
                        adjustment_factor=row.next_adjustment_factor,
                        entry_bar=bar_index,
                        entry_adjusted_price=(
                            cost.effective_price * row.next_adjustment_factor
                        ),
                        peak_adjusted_price=(
                            event.price * row.next_adjustment_factor
                        ),
                        theme=_theme_key(row),
                        entry_time=event.time,
                    )
                else:
                    total = position.shares + shares
                    adjusted_cost = (
                        cost.effective_price * row.next_adjustment_factor
                    )
                    position.entry_adjusted_price = (
                        position.entry_adjusted_price * position.shares
                        + adjusted_cost * shares
                    ) / total
                    position.shares = total
                    position.unsettled_shares += shares
                trades.append(_trade(row, event, shares, cost))

        for symbol, position in positions.items():
            row = rows.get(symbol)
            if row is None:
                continue
            bars = day_bars.get(symbol)
            if bars is not None:
                eligible = bars.filter(
                    pl.col("trade_time") > position.entry_time
                )
                if not eligible.is_empty():
                    position.peak_adjusted_price = max(
                        position.peak_adjusted_price,
                        float(eligible["high"].max())
                        * row.next_adjustment_factor,
                    )
            position.mark_price = row.next_close
        equity = cash + sum(
            position.shares * position.mark_price
            for position in positions.values()
        )
        curve.append(
            EquityPoint(
                date=trade_date,
                equity=equity,
                cash=cash,
                positions=len(positions),
            )
        )

    if missing:
        raise MissingMinuteDataError(missing)
    return _summarize(
        config,
        curve,
        trades,
        turnover,
        hold_lengths,
        positions,
        closed_trades,
    )
