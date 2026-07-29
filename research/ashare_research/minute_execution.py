"""A-share minute-bar execution simulation for V1.1 rebound research.

Models real-world A-share trading constraints:
- T+1: shares bought on D+1 cannot be sold until D+2.
- Lot size: 100 shares minimum, round down.
- Price limits: cannot buy at limit-up, cannot sell at limit-down.
- Suspension: no execution on suspended days.
- Bar-close confirmation: signal confirmed at bar close, execution at next bar open.
- Capacity: single order cannot exceed 1% of execution bar's turnover.
- Fees: shared buy/sell commission, sell stamp duty, and minimum commission.
- Slippage: shared production cost model.
- Fail closed: missing bars, adj_factor, or limit prices → no_fill.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import polars as pl

from .cost_config import load_cost_model
from .trading_constraints import load_trading_constraints

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_COST_MODEL = load_cost_model()
_TRADING_CONSTRAINTS = load_trading_constraints()
DEFAULT_SLIPPAGE_BPS = _COST_MODEL.base_slippage_bps
DEFAULT_CAPACITY_PCT = _TRADING_CONSTRAINTS.max_order_bar_amount_pct
LOT_SIZE = _TRADING_CONSTRAINTS.lot_size


@dataclass(frozen=True)
class ExecutionConfig:
    # Legacy symmetric fee override. None uses the shared side-specific model.
    fee_bps: float | None = None
    slippage_bps: float = DEFAULT_SLIPPAGE_BPS
    capacity_pct: float = DEFAULT_CAPACITY_PCT
    lot_size: int = LOT_SIZE
    buy_commission_bps: float = _COST_MODEL.buy_commission_bps
    sell_commission_bps: float = _COST_MODEL.sell_commission_bps
    stamp_duty_bps: float = _COST_MODEL.stamp_duty_bps
    minimum_commission_yuan: float = _COST_MODEL.minimum_commission_yuan

    def __post_init__(self) -> None:
        fee_values = (
            self.slippage_bps,
            self.buy_commission_bps,
            self.sell_commission_bps,
            self.stamp_duty_bps,
            self.minimum_commission_yuan,
        )
        if any(not math.isfinite(value) or value < 0 for value in fee_values):
            raise ValueError("fees and slippage must be non-negative")
        if self.fee_bps is not None and (
            not math.isfinite(self.fee_bps) or self.fee_bps < 0
        ):
            raise ValueError("fees and slippage must be non-negative")
        if (
            not math.isfinite(self.capacity_pct)
            or self.capacity_pct <= 0
            or self.capacity_pct > 100
        ):
            raise ValueError("capacity_pct must be in (0, 100]")
        if (
            isinstance(self.lot_size, bool)
            or not isinstance(self.lot_size, int)
            or self.lot_size <= 0
        ):
            raise ValueError("lot_size must be positive")


# ---------------------------------------------------------------------------
# Trade result
# ---------------------------------------------------------------------------


@dataclass
class TradeResult:
    event_id: str
    symbol: str
    decision_date: str  # D date
    entry_signal_time: str = ""
    entry_time: str = ""
    entry_price_raw: float = 0.0
    entry_price_with_cost: float = 0.0
    entry_commission_yuan: float = 0.0
    entry_slippage_yuan: float = 0.0
    shares: int = 0
    entry_reason: str = ""
    exit_signal_time: str = ""
    exit_time: str = ""
    exit_price_raw: float = 0.0
    exit_price_with_cost: float = 0.0
    exit_commission_yuan: float = 0.0
    exit_stamp_duty_yuan: float = 0.0
    exit_slippage_yuan: float = 0.0
    exit_reason: str = ""
    gross_return: float = 0.0
    net_return: float = 0.0
    pnl_per_10000: float = 0.0
    mfe: float = 0.0  # max favorable excursion
    mae: float = 0.0  # max adverse excursion
    t1_blocked_stop: bool = False
    pending_exit_bars: int = 0
    no_fill_reason: str = ""

    @property
    def filled(self) -> bool:
        return self.no_fill_reason == "" and self.shares > 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "symbol": self.symbol,
            "decision_date": self.decision_date,
            "entry_signal_time": self.entry_signal_time,
            "entry_time": self.entry_time,
            "entry_price_raw": self.entry_price_raw,
            "entry_price_with_cost": self.entry_price_with_cost,
            "entry_commission_yuan": self.entry_commission_yuan,
            "entry_slippage_yuan": self.entry_slippage_yuan,
            "shares": self.shares,
            "entry_reason": self.entry_reason,
            "exit_signal_time": self.exit_signal_time,
            "exit_time": self.exit_time,
            "exit_price_raw": self.exit_price_raw,
            "exit_price_with_cost": self.exit_price_with_cost,
            "exit_commission_yuan": self.exit_commission_yuan,
            "exit_stamp_duty_yuan": self.exit_stamp_duty_yuan,
            "exit_slippage_yuan": self.exit_slippage_yuan,
            "exit_reason": self.exit_reason,
            "gross_return": self.gross_return,
            "net_return": self.net_return,
            "pnl_per_10000": self.pnl_per_10000,
            "mfe": self.mfe,
            "mae": self.mae,
            "t1_blocked_stop": self.t1_blocked_stop,
            "pending_exit_bars": self.pending_exit_bars,
            "no_fill_reason": self.no_fill_reason,
        }


# ---------------------------------------------------------------------------
# Price limit helpers
# ---------------------------------------------------------------------------


def price_limit_fraction(ts_code: str) -> float:
    """Return the price limit fraction for a given stock based on its board."""
    symbol = ts_code.split(".")[0]
    if symbol.startswith("688"):
        return 0.20  # STAR Market ±20%
    if symbol.startswith("300") or symbol.startswith("301"):
        return 0.20  # ChiNext ±20%
    if symbol.startswith(("4", "8")):
        return 0.30  # BSE ±30%
    return 0.10  # Main board ±10%


def is_limit_up(price: float, up_limit: float, slack: float = 0.0005) -> bool:
    """Check against the exact Tushare `stk_limit.up_limit` price."""
    if not math.isfinite(price) or not math.isfinite(up_limit) or up_limit <= 0:
        return False
    return price >= up_limit * (1 - slack)


def is_limit_down(price: float, down_limit: float, slack: float = 0.0005) -> bool:
    """Check against the exact Tushare `stk_limit.down_limit` price."""
    if not math.isfinite(price) or not math.isfinite(down_limit) or down_limit <= 0:
        return False
    return price <= down_limit * (1 + slack)


# ---------------------------------------------------------------------------
# Core execution engine
# ---------------------------------------------------------------------------


def _commission_per_share(
    price: float,
    shares: int | None,
    commission_bps: float,
    minimum_commission_yuan: float,
) -> float:
    proportional = price * commission_bps / 10000.0
    if shares is None or shares <= 0:
        return proportional
    total = max(
        price * shares * commission_bps / 10000.0,
        minimum_commission_yuan,
    )
    return total / shares


def compute_entry_cost(
    price: float,
    config: ExecutionConfig,
    shares: int | None = None,
) -> float:
    """Compute effective entry price including fees and slippage."""
    slippage = price * (config.slippage_bps / 10000.0)
    commission_bps = (
        config.fee_bps
        if config.fee_bps is not None
        else config.buy_commission_bps
    )
    fee = _commission_per_share(
        price,
        shares,
        commission_bps,
        0.0 if config.fee_bps is not None else config.minimum_commission_yuan,
    )
    return price + slippage + fee


def compute_exit_cost(
    price: float,
    config: ExecutionConfig,
    shares: int | None = None,
) -> float:
    """Compute effective exit price after fees and slippage."""
    slippage = price * (config.slippage_bps / 10000.0)
    commission_bps = (
        config.fee_bps
        if config.fee_bps is not None
        else config.sell_commission_bps
    )
    fee = _commission_per_share(
        price,
        shares,
        commission_bps,
        0.0 if config.fee_bps is not None else config.minimum_commission_yuan,
    )
    stamp = (
        0.0
        if config.fee_bps is not None
        else price * config.stamp_duty_bps / 10000.0
    )
    return price - slippage - fee - stamp


def round_lots(shares: float, lot_size: int = LOT_SIZE) -> int:
    """Round down to nearest lot size."""
    if not math.isfinite(shares) or shares <= 0:
        return 0
    return int(shares // lot_size) * lot_size


def check_capacity(order_amount: float, bar_amount: float, config: ExecutionConfig) -> bool:
    """Check if order amount is within capacity limit."""
    if (
        not math.isfinite(order_amount)
        or not math.isfinite(bar_amount)
        or order_amount <= 0
        or bar_amount <= 0
    ):
        return False
    return order_amount <= bar_amount * (config.capacity_pct / 100.0)


def _bar_open_time(trade_time: str) -> str | None:
    """Convert a 5-minute provider close label to the interval open time."""
    try:
        close_time = datetime.strptime(trade_time, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return (close_time - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")


def _decision_close_time(decision_date: str) -> str | None:
    """Return the D-close timestamp at which the candidate became observable."""
    compact = decision_date.strip().replace("-", "")
    try:
        parsed = datetime.strptime(compact, "%Y%m%d")
    except ValueError:
        return None
    return parsed.strftime("%Y-%m-%d 15:00:00")


def simulate_trade(
    event_id: str,
    symbol: str,
    ts_code: str,
    decision_date: str,
    entry_bars: pl.DataFrame,
    exit_bars: pl.DataFrame,
    entry_signal_idx: int,
    hold_days: int,
    stop_loss_pct: float,
    config: ExecutionConfig,
    capital_per_trade: float = 10000.0,
    entry_up_limit: float | None = None,
    exit_down_limits: dict[str, float] | None = None,
    suspended_dates: set[str] | None = None,
    adj_factors: dict[str, float] | None = None,
    eligible_exit_date: str | None = None,
) -> TradeResult:
    """Simulate a single trade with full A-share constraints.

    Args:
        event_id: Unique event identifier.
        symbol: Bare symbol (e.g. '000001').
        ts_code: Full ts_code (e.g. '000001.SZ').
        decision_date: D date (YYYYMMDD).
        entry_bars: 5min bars for D+1 (entry day), sorted by trade_time.
        exit_bars: 5min bars for exit period, sorted by trade_time.
        entry_signal_idx: Index of the bar where entry signal is confirmed.
            Execution happens at the NEXT bar's open.
        hold_days: Number of trading days to hold.
        stop_loss_pct: Stop loss percentage (e.g. 0.05 for 5%).
        config: Execution configuration.
        capital_per_trade: Capital allocated per trade.
        entry_up_limit: Exact D+1 up-limit price from `stk_limit`.
        exit_down_limits: Exact per-date down-limit prices from `stk_limit`.
        suspended_dates: Set of suspended dates (YYYYMMDD).
        adj_factors: {date: adj_factor} for cross-ex-date return calc.
        eligible_exit_date: First market date allowed by the fixed hold period.
    """
    result = TradeResult(
        event_id=event_id,
        symbol=symbol,
        decision_date=decision_date,
    )
    suspended = suspended_dates or set()
    down_limits = exit_down_limits or {}
    adj_factors_map = adj_factors or {}
    if (
        hold_days <= 0
        or not math.isfinite(stop_loss_pct)
        or not 0 < stop_loss_pct < 1
        or not math.isfinite(capital_per_trade)
        or capital_per_trade <= 0
        or entry_signal_idx < -1
    ):
        result.no_fill_reason = "no_fill_invalid_parameters"
        return result

    # --- ENTRY ---
    if entry_bars.height == 0:
        result.no_fill_reason = "no_fill_data_missing"
        return result

    # Entry execution: signal confirmed at bar[entry_signal_idx] close,
    # execute at bar[entry_signal_idx + 1] open
    exec_idx = 0 if entry_signal_idx < 0 else entry_signal_idx + 1
    if exec_idx >= entry_bars.height:
        result.no_fill_reason = "no_fill_no_next_bar"
        return result

    entry_exec_bar = entry_bars.row(exec_idx, named=True)
    entry_date = str(entry_exec_bar["trade_date"])
    entry_bar_time = str(entry_exec_bar["trade_time"])
    entry_time = _bar_open_time(entry_bar_time)
    if entry_time is None:
        result.no_fill_reason = "no_fill_invalid_bar"
        return result

    # Check suspension
    if entry_date in suspended:
        result.no_fill_reason = "no_fill_suspended"
        return result

    entry_open = float(entry_exec_bar["open"])
    entry_bar_amount = float(entry_exec_bar.get("amount", 0))
    if not math.isfinite(entry_open) or entry_open <= 0:
        result.no_fill_reason = "no_fill_invalid_bar"
        return result

    if (
        entry_up_limit is None
        or not math.isfinite(entry_up_limit)
        or entry_up_limit <= 0
    ):
        result.no_fill_reason = "no_fill_missing_limit_price"
        return result

    if is_limit_up(entry_open, entry_up_limit):
        result.no_fill_reason = "no_fill_limit_up"
        return result

    entry_adj = adj_factors_map.get(entry_date)
    if entry_adj is None or not math.isfinite(entry_adj) or entry_adj <= 0:
        result.no_fill_reason = "no_fill_missing_adj_factor"
        return result

    # Compute shares
    estimated_entry_price = compute_entry_cost(entry_open, config)
    raw_shares = capital_per_trade / estimated_entry_price
    shares = round_lots(raw_shares, config.lot_size)
    entry_price_with_cost = compute_entry_cost(entry_open, config, shares)
    while shares > 0 and entry_price_with_cost * shares > capital_per_trade:
        shares -= config.lot_size
        if shares > 0:
            entry_price_with_cost = compute_entry_cost(entry_open, config, shares)
    if shares <= 0:
        result.no_fill_reason = "no_fill_lot_size"
        return result
    order_amount = shares * entry_open
    if not check_capacity(order_amount, entry_bar_amount, config):
        result.no_fill_reason = "no_fill_capacity"
        return result

    if entry_signal_idx < 0:
        decision_close_time = _decision_close_time(decision_date)
        if decision_close_time is None:
            result.no_fill_reason = "no_fill_invalid_parameters"
            return result
        result.entry_signal_time = decision_close_time
        result.entry_reason = "next_open"
    else:
        result.entry_signal_time = str(
            entry_bars.row(entry_signal_idx, named=True)["trade_time"]
        )
        result.entry_reason = "confirmed"
    result.entry_time = entry_time
    result.entry_price_raw = entry_open
    result.entry_price_with_cost = entry_price_with_cost
    result.entry_commission_yuan = max(
        entry_open
        * shares
        * (
            config.fee_bps
            if config.fee_bps is not None
            else config.buy_commission_bps
        )
        / 10000.0,
        0.0
        if config.fee_bps is not None
        else config.minimum_commission_yuan,
    )
    result.entry_slippage_yuan = (
        entry_open * shares * config.slippage_bps / 10000.0
    )
    result.shares = shares
    if exit_bars.height == 0:
        result.no_fill_reason = "no_fill_no_exit_data"
        return result

    mfe = 0.0
    mae = 0.0
    t1_blocked = False
    stop_loss_price = entry_price_with_cost * (1 - stop_loss_pct)
    pending_bars = 0
    all_bars = (
        # Provider timestamps label the interval close. Start with the actual
        # execution bar, not the preceding confirmation bar whose close may
        # have the same clock value as the execution bar's open.
        exit_bars.filter(pl.col("trade_time") >= entry_bar_time)
        .unique(subset=["ts_code", "trade_time"], keep="last")
        .sort("trade_time")
    )
    if all_bars.height == 0:
        result.no_fill_reason = "no_fill_no_exit_data"
        return result

    post_entry_dates = sorted(
        date_value
        for date_value in set(all_bars["trade_date"].cast(pl.String).to_list())
        if date_value > entry_date
    )
    if not post_entry_dates:
        result.no_fill_reason = "no_fill_no_exit_data"
        return result
    if len(post_entry_dates) >= hold_days:
        required_exit_date = post_entry_dates[hold_days - 1]
        if eligible_exit_date is None or eligible_exit_date < required_exit_date:
            eligible_exit_date = required_exit_date
    else:
        # Keep processing so a stop can still execute on the earliest legal
        # post-entry day. A fixed-hold exit remains unavailable.
        eligible_exit_date = "99999999"

    stop_triggered = False
    stop_trigger_time = ""
    stop_execute_from_index: int | None = None
    exit_price_raw = 0.0
    exit_time = ""
    exit_date = ""
    exit_signal_time = ""
    exit_reason = ""

    for i in range(all_bars.height):
        bar = all_bars.row(i, named=True)
        bar_date = str(bar["trade_date"])
        bar_time = str(bar["trade_time"])
        bar_close = float(bar["close"])
        bar_open = float(bar["open"])
        bar_high = float(bar["high"])
        bar_low = float(bar["low"])
        if (
            not all(
                math.isfinite(value)
                for value in (bar_open, bar_high, bar_low, bar_close)
            )
            or min(bar_open, bar_high, bar_low, bar_close) <= 0
        ):
            result.no_fill_reason = "no_fill_invalid_bar"
            result.mfe = mfe
            result.mae = mae
            result.t1_blocked_stop = t1_blocked
            return result

        factor = adj_factors_map.get(bar_date)
        if factor is None or not math.isfinite(factor) or factor <= 0:
            result.no_fill_reason = "no_fill_missing_adj_factor"
            result.mfe = mfe
            result.mae = mae
            result.t1_blocked_stop = t1_blocked
            return result

        is_entry_day = bar_date == entry_date
        stop_due = (
            stop_triggered
            and not is_entry_day
            and stop_execute_from_index is not None
            and i >= stop_execute_from_index
        )
        normal_due = not stop_triggered and bar_date >= eligible_exit_date

        # Exit decisions happen at bar open, before observing this bar's close.
        if stop_due or normal_due:
            if bar_date in suspended:
                pending_bars += 1
                continue
            down_limit = down_limits.get(bar_date)
            if (
                down_limit is None
                or not math.isfinite(down_limit)
                or down_limit <= 0
            ):
                result.no_fill_reason = "no_fill_missing_limit_price"
                result.mfe = mfe
                result.mae = mae
                result.t1_blocked_stop = t1_blocked
                result.pending_exit_bars = pending_bars
                return result
            if is_limit_down(bar_open, down_limit):
                pending_bars += 1
                continue
            if not check_capacity(result.shares * bar_open, float(bar.get("amount", 0)), config):
                pending_bars += 1
                continue
            exit_price_raw = bar_open
            exit_time = _bar_open_time(bar_time) or ""
            if not exit_time:
                result.no_fill_reason = "no_fill_invalid_bar"
                return result
            exit_date = bar_date
            if stop_due:
                exit_signal_time = stop_trigger_time
                exit_reason = "stop_loss"
            else:
                exit_reason = f"hold_{hold_days}d_expire"
            break

        adjusted_close = bar_close * factor / entry_adj
        adjusted_high = bar_high * factor / entry_adj
        adjusted_low = bar_low * factor / entry_adj
        mfe = max(
            mfe,
            (adjusted_high - entry_price_with_cost) / entry_price_with_cost,
        )
        mae = min(
            mae,
            (adjusted_low - entry_price_with_cost) / entry_price_with_cost,
        )

        if not stop_triggered and adjusted_close <= stop_loss_price:
            stop_triggered = True
            stop_trigger_time = bar_time
            if is_entry_day:
                t1_blocked = True
                for next_index in range(i + 1, all_bars.height):
                    next_date = str(all_bars.row(next_index, named=True)["trade_date"])
                    if next_date > entry_date:
                        stop_execute_from_index = next_index
                        break
            else:
                stop_execute_from_index = i + 1

    if not exit_time:
        result.no_fill_reason = "no_fill_no_exit_opportunity"
        result.mfe = mfe
        result.mae = mae
        result.t1_blocked_stop = t1_blocked
        result.pending_exit_bars = pending_bars
        return result

    exit_price_with_cost = compute_exit_cost(exit_price_raw, config, result.shares)
    exit_adj = adj_factors_map.get(exit_date)
    if exit_adj is None or not math.isfinite(exit_adj) or exit_adj <= 0:
        result.no_fill_reason = "no_fill_missing_adj_factor"
        result.mfe = mfe
        result.mae = mae
        result.t1_blocked_stop = t1_blocked
        return result

    entry_adj_price = entry_open * entry_adj
    exit_adj_price = exit_price_raw * exit_adj
    gross_return = (exit_adj_price - entry_adj_price) / entry_adj_price

    # Net return uses cost-adjusted prices with adj factor
    entry_cost_adj = entry_price_with_cost * entry_adj
    exit_cost_adj = exit_price_with_cost * exit_adj
    net_return = (exit_cost_adj - entry_cost_adj) / entry_cost_adj
    invested_capital = entry_price_with_cost * shares
    normalized_scale = 10000.0 / capital_per_trade if capital_per_trade > 0 else 0.0
    pnl_per_10000 = net_return * invested_capital * normalized_scale

    result.exit_signal_time = exit_signal_time
    result.exit_time = exit_time
    result.exit_price_raw = exit_price_raw
    result.exit_price_with_cost = exit_price_with_cost
    result.exit_commission_yuan = max(
        exit_price_raw
        * result.shares
        * (
            config.fee_bps
            if config.fee_bps is not None
            else config.sell_commission_bps
        )
        / 10000.0,
        0.0
        if config.fee_bps is not None
        else config.minimum_commission_yuan,
    )
    result.exit_stamp_duty_yuan = (
        0.0
        if config.fee_bps is not None
        else exit_price_raw
        * result.shares
        * config.stamp_duty_bps
        / 10000.0
    )
    result.exit_slippage_yuan = (
        exit_price_raw * result.shares * config.slippage_bps / 10000.0
    )
    result.exit_reason = exit_reason
    result.gross_return = gross_return
    result.net_return = net_return
    result.pnl_per_10000 = pnl_per_10000
    result.mfe = mfe
    result.mae = mae
    result.t1_blocked_stop = t1_blocked
    result.pending_exit_bars = pending_bars

    return result


def simulate_trade_simple(
    event_id: str,
    symbol: str,
    ts_code: str,
    decision_date: str,
    bars_d1: pl.DataFrame,
    bars_exit_period: pl.DataFrame,
    entry_signal_idx: int,
    hold_days: int,
    stop_loss_pct: float = 0.05,
    config: ExecutionConfig | None = None,
    capital_per_trade: float = 10000.0,
    entry_up_limit: float | None = None,
    exit_down_limits: dict[str, float] | None = None,
    suspended_dates: set[str] | None = None,
    adj_factors: dict[str, float] | None = None,
    eligible_exit_date: str | None = None,
) -> TradeResult:
    """Convenience wrapper for simulate_trade with sensible defaults."""
    if config is None:
        config = ExecutionConfig()
    return simulate_trade(
        event_id=event_id,
        symbol=symbol,
        ts_code=ts_code,
        decision_date=decision_date,
        entry_bars=bars_d1,
        exit_bars=bars_exit_period,
        entry_signal_idx=entry_signal_idx,
        hold_days=hold_days,
        stop_loss_pct=stop_loss_pct,
        config=config,
        capital_per_trade=capital_per_trade,
        entry_up_limit=entry_up_limit,
        exit_down_limits=exit_down_limits,
        suspended_dates=suspended_dates,
        adj_factors=adj_factors,
        eligible_exit_date=eligible_exit_date,
    )
