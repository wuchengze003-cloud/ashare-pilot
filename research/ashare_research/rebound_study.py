"""Low-position rebound event study for V1.1 research.

Implements the pre-registered event study defined in the development plan §M4:
- Public event definition (D-close conditions)
- 3 pre-registered entry strategies: next_open, vwap_reclaim, higher_low_breakout
- 3 pre-registered holding periods: 1, 3, 5 days
- Development / validation / frozen time splits
- Strategy selection rules (not by win rate)
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

from .minute_execution import (
    TradeResult,
)

# ---------------------------------------------------------------------------
# Event definition thresholds (pre-registered, not tunable)
# ---------------------------------------------------------------------------

MIN_LISTING_DAYS = 60
MAX_60D_POSITION_PCT = 25.0  # 60-day price range position <= 25%
MIN_60D_DRAWDOWN_PCT = 20.0  # drawdown from 60-day high >= 20%
MAX_5D_RETURN_PCT = -6.0  # recent 5-day return <= -6%
MIN_20D_AVG_AMOUNT = 50_000.0  # 20-day avg turnover >= 50M CNY (Tushare amount unit: 千元)
GAP_UP_CANCEL_PCT = 3.0  # D+1 gap up > 3% cancels entry
STOP_LOSS_PCT = 5.0  # initial stop loss 5%
LATEST_ENTRY_TIME = "14:30:00"  # no new entries after 14:30

# Time splits
DEV_START = "2025-01-01"
DEV_END = "2025-09-30"
VAL_START = "2025-10-01"
VAL_END = "2026-02-23"
FROZEN_START = "2026-02-24"

# Strategy selection
MIN_VAL_TRADES = 30
MIN_FROZEN_TRADING_DAYS = 250
MIN_FROZEN_EVENTS = 100

# Portfolio sizing
RISK_BUDGET_PCT = 0.5  # 0.5% of portfolio per trade risk
POSITION_CAP_PCT = 5.0  # 5% max single position
PORTFOLIO_CAPITAL = 100_000.0


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EntryRule:
    gap_up_pct: float = GAP_UP_CANCEL_PCT
    min_confirm_time: str = "09:45:00"
    consecutive_bars: int = 2
    latest_entry_time: str = LATEST_ENTRY_TIME


@dataclass
class ReboundEvent:
    event_id: str
    symbol: str
    ts_code: str
    decision_date: str  # D date YYYYMMDD
    close_d: float
    position_60d_pct: float
    drawdown_60d_pct: float
    return_5d_pct: float
    avg_amount_20d: float
    adj_factor_d: float | None = None


@dataclass
class StrategyResult:
    strategy_name: str
    hold_days: int
    events: int = 0
    filled_trades: int = 0
    no_fill_count: int = 0
    no_fill_reasons: dict[str, int] = field(default_factory=dict)
    win_rate: float = 0.0
    mean_net_return: float = 0.0
    median_net_return: float = 0.0
    mean_win: float = 0.0
    mean_loss: float = 0.0
    profit_loss_ratio: float = 0.0
    profit_factor: float = 0.0
    mean_mfe: float = 0.0
    mean_mae: float = 0.0
    cvar_5pct: float = 0.0
    max_drawdown: float = 0.0
    expected_profit_per_10k: float = 0.0
    expected_profit_per_100k: float = 0.0
    return_cvar_ratio: float = 0.0
    bootstrap_ci_low: float = 0.0
    bootstrap_ci_high: float = 0.0
    trades: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        def finite_or_none(value: float, digits: int) -> float | None:
            return round(value, digits) if math.isfinite(value) else None

        d = {
            "strategy_name": self.strategy_name,
            "hold_days": self.hold_days,
            "events": self.events,
            "filled_trades": self.filled_trades,
            "no_fill_count": self.no_fill_count,
            "no_fill_reasons": self.no_fill_reasons,
            "win_rate": round(self.win_rate, 6),
            "mean_net_return": round(self.mean_net_return, 6),
            "median_net_return": round(self.median_net_return, 6),
            "mean_win": round(self.mean_win, 6),
            "mean_loss": round(self.mean_loss, 6),
            "profit_loss_ratio": finite_or_none(self.profit_loss_ratio, 4),
            "profit_factor": finite_or_none(self.profit_factor, 4),
            "mean_mfe": round(self.mean_mfe, 6),
            "mean_mae": round(self.mean_mae, 6),
            "cvar_5pct": round(self.cvar_5pct, 6),
            "max_drawdown": round(self.max_drawdown, 6),
            "expected_profit_per_10k": round(self.expected_profit_per_10k, 4),
            "expected_profit_per_100k": round(self.expected_profit_per_100k, 4),
            "return_cvar_ratio": round(self.return_cvar_ratio, 4),
            "bootstrap_ci_low": round(self.bootstrap_ci_low, 6),
            "bootstrap_ci_high": round(self.bootstrap_ci_high, 6),
        }
        return d


@dataclass
class StudySummary:
    stage: str
    config_hash: str
    run_at: str
    coverage_sha256: str = ""
    daily_coverage_sha256: str = ""
    universe_sha256: str = ""
    strategies: list[StrategyResult] = field(default_factory=list)
    selected_strategy: str = ""
    verdict: str = ""  # "viable", "no_viable_strategy", "insufficient_evidence"
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "config_hash": self.config_hash,
            "run_at": self.run_at,
            "coverage_sha256": self.coverage_sha256,
            "daily_coverage_sha256": self.daily_coverage_sha256,
            "universe_sha256": self.universe_sha256,
            "strategies": [s.to_dict() for s in self.strategies],
            "selected_strategy": self.selected_strategy,
            "verdict": self.verdict,
            "note": self.note,
        }


# ---------------------------------------------------------------------------
# Event detection (D-close conditions)
# ---------------------------------------------------------------------------


def detect_events(
    daily_df: pl.DataFrame,
    universe_symbols: set[str] | dict[str, tuple[str, str]],
    start_date: str,
    end_date: str,
    min_listing_days: int = MIN_LISTING_DAYS,
    max_60d_position_pct: float = MAX_60D_POSITION_PCT,
    min_60d_drawdown_pct: float = MIN_60D_DRAWDOWN_PCT,
    max_5d_return_pct: float = MAX_5D_RETURN_PCT,
    min_20d_avg_amount: float = MIN_20D_AVG_AMOUNT,
) -> list[ReboundEvent]:
    """Detect low-position rebound events using D-close data only.

    Args:
        daily_df: Daily bars with columns: ts_code, trade_date, open, high, low,
                  close, volume, amount, adj_factor, list_date, is_st, suspended.
                  MUST include lookback data before start_date for 60-day window.
        universe_symbols: Set of bare symbols allowed (point-in-time filtered).
        start_date: YYYYMMDD start of detection window (events emitted from here).
        end_date: YYYYMMDD end of detection window.

    Returns:
        List of qualifying events within [start_date, end_date].
    """
    events: list[ReboundEvent] = []

    # CRITICAL: Do NOT filter by start_date here. We need the full history
    # (including lookback) for the 60-day window calculation.
    # Only emit events within [start_date, end_date].
    df = daily_df.filter(pl.col("trade_date") <= end_date)

    # Group by symbol
    symbols = sorted(df["ts_code"].unique().to_list())

    for ts_code in symbols:
        symbol = ts_code.split(".")[0]
        if symbol not in universe_symbols:
            continue

        sym_df = df.filter(pl.col("ts_code") == ts_code).sort("trade_date")
        if sym_df.height < min_listing_days:
            continue

        rows = sym_df.to_dicts()
        for i in range(max(0, min_listing_days - 1), len(rows)):
            row = rows[i]
            trade_date = str(row["trade_date"])

            # CRITICAL: Only emit events within [start_date, end_date].
            # Lookback data before start_date is used for window calculation only.
            if trade_date < start_date:
                continue
            if isinstance(universe_symbols, dict):
                active_from, active_until = universe_symbols[symbol]
                if trade_date < active_from or trade_date > active_until:
                    continue

            # Reference/suspension metadata is mandatory. Unknown is not non-ST.
            if row.get("is_st") is not False:
                continue
            if row.get("suspended") is not False:
                continue

            close_d = float(row["close"])
            if not math.isfinite(close_d) or close_d <= 0:
                continue

            # CRITICAL: Use adj_factor for cross-ex-date price comparison.
            # Raw prices cannot be compared across ex-dividend dates.
            adj_d = row.get("adj_factor")
            if (
                adj_d is None
                or not math.isfinite(float(adj_d))
                or float(adj_d) <= 0
            ):
                # Missing adj_factor, cannot research this event
                continue
            adj_d = float(adj_d)

            # The listing-age gate is configurable, but the event metrics are
            # always based on the pre-registered 60-session window.
            metric_window_days = min(60, min_listing_days)
            window = rows[max(0, i - metric_window_days + 1): i + 1]
            if len(window) < metric_window_days:
                continue

            # Adjust prices by adj_factor for valid comparison
            closes_60d: list[float] = []
            highs_60d: list[float] = []
            lows_60d: list[float] = []
            window_complete = True
            for w in window:
                w_adj = w.get("adj_factor")
                if (
                    w_adj is None
                    or not math.isfinite(float(w_adj))
                    or float(w_adj) <= 0
                ):
                    window_complete = False
                    break
                w_adj = float(w_adj)
                c = float(w.get("close", 0))
                h = float(w.get("high", 0))
                lo = float(w.get("low", 0))
                if (
                    not all(math.isfinite(value) for value in (c, h, lo))
                    or min(c, h, lo) <= 0
                ):
                    window_complete = False
                    break
                closes_60d.append(c * w_adj)
                highs_60d.append(h * w_adj)
                lows_60d.append(lo * w_adj)

            if not window_complete or len(closes_60d) != len(window):
                continue

            # Adjusted close for D day
            close_d_adj = close_d * adj_d
            high_60d = max(highs_60d) if highs_60d else close_d_adj
            low_60d = min(lows_60d) if lows_60d else close_d_adj
            highest_close_60d = max(closes_60d) if closes_60d else close_d_adj

            # Condition 3: 60-day price range position <= 25%
            price_range = high_60d - low_60d
            if price_range <= 0:
                continue
            position_pct = ((close_d_adj - low_60d) / price_range) * 100.0
            if position_pct > max_60d_position_pct:
                continue

            # Condition 4: drawdown from the 60-day highest CLOSE >= 20%.
            drawdown_pct = (
                (highest_close_60d - close_d_adj) / highest_close_60d
            ) * 100.0
            if drawdown_pct < min_60d_drawdown_pct:
                continue

            # Condition 5: recent 5-day return <= -6% (use adj prices)
            if i >= 5:
                row_5d_ago = rows[i - 5]
                close_5d_ago = float(row_5d_ago.get("close", 0))
                adj_5d_ago = row_5d_ago.get("adj_factor")
                if (
                    math.isfinite(close_5d_ago)
                    and close_5d_ago > 0
                    and adj_5d_ago is not None
                    and math.isfinite(float(adj_5d_ago))
                    and float(adj_5d_ago) > 0
                ):
                    close_5d_adj = close_5d_ago * float(adj_5d_ago)
                    return_5d_pct = ((close_d_adj - close_5d_adj) / close_5d_adj) * 100.0
                else:
                    continue
            else:
                continue
            if return_5d_pct > max_5d_return_pct:
                continue

            # Condition 6: 20-day avg amount >= 50M
            recent_20 = rows[max(0, i - 19): i + 1]
            amounts = [float(w.get("amount", 0)) for w in recent_20]
            if not all(math.isfinite(value) and value >= 0 for value in amounts):
                continue
            avg_amount = sum(amounts) / len(amounts) if amounts else 0
            if avg_amount < min_20d_avg_amount:
                continue

            # adj_factor already checked above (adj_d)
            event_id = f"{symbol}_{trade_date}"
            events.append(ReboundEvent(
                event_id=event_id,
                symbol=symbol,
                ts_code=ts_code,
                decision_date=trade_date,
                close_d=close_d,
                position_60d_pct=position_pct,
                drawdown_60d_pct=drawdown_pct,
                return_5d_pct=return_5d_pct,
                avg_amount_20d=avg_amount,
                adj_factor_d=adj_d,
            ))

    return sorted(events, key=lambda event: (event.decision_date, event.ts_code))


# ---------------------------------------------------------------------------
# Entry signal detection (D+1 intraday)
# ---------------------------------------------------------------------------


def detect_next_open_entry(
    bars_d1: pl.DataFrame,
    prev_close: float,
    ts_code: str,
    rule: EntryRule | None = None,
) -> int | None:
    """next_open strategy: buy at D+1 open (signal idx = -1 means use first bar).

    Returns the signal index (the bar BEFORE execution bar).
    For next_open, we use a virtual signal at index -1 (before first bar),
    meaning execution at bar[0] open.
    Returns 0 if first bar is valid (we'll execute at bar[1] open in the engine,
    but for next_open we special-case to execute at bar[0]).
    """
    if bars_d1.height == 0:
        return None
    rule = rule or EntryRule()

    first_bar = bars_d1.row(0, named=True)
    first_open = float(first_bar["open"])

    # Cancel if gap up > 3%
    if prev_close > 0:
        gap_pct = ((first_open - prev_close) / prev_close) * 100.0
        if gap_pct > rule.gap_up_pct:
            return None

    # For next_open, signal is "at market open" - we return -1 as special marker
    # The execution engine will handle this as "execute at bar[0] open"
    return -1


def detect_vwap_reclaim_entry(
    bars_d1: pl.DataFrame,
    prev_close: float,
    ts_code: str,
    rule: EntryRule | None = None,
) -> int | None:
    """vwap_reclaim: after 09:45, 2 consecutive 5min closes above cumulative VWAP.

    Returns the index of the confirmation bar (execution at next bar open).
    """
    if bars_d1.height == 0:
        return None
    rule = rule or EntryRule()

    # Check gap-up cancel
    first_bar = bars_d1.row(0, named=True)
    first_open = float(first_bar["open"])
    if prev_close > 0:
        gap_pct = ((first_open - prev_close) / prev_close) * 100.0
        if gap_pct > rule.gap_up_pct:
            return None

    # Compute cumulative VWAP
    cum_amount = 0.0
    cum_volume = 0.0
    consecutive_above = 0

    for i in range(bars_d1.height):
        bar = bars_d1.row(i, named=True)
        bar_time = str(bar["trade_time"])
        time_part = bar_time[11:19] if len(bar_time) > 11 else ""

        close = float(bar["close"])
        volume = float(bar.get("volume", 0))
        amount = float(bar.get("amount", 0))

        cum_volume += volume
        cum_amount += amount

        # Only check after 09:45
        if time_part < rule.min_confirm_time:
            consecutive_above = 0
            continue

        # No new entries after 14:30
        if time_part > rule.latest_entry_time:
            break

        # VWAP
        vwap = cum_amount / cum_volume if cum_volume > 0 else close

        if close > vwap:
            consecutive_above += 1
        else:
            consecutive_above = 0

        # Need 2 consecutive closes above VWAP
        if consecutive_above >= rule.consecutive_bars:
            # Confirm at this bar's close, execute at next bar open
            if i + 1 < bars_d1.height:
                next_time = _bar_open_clock(
                    str(bars_d1.row(i + 1, named=True)["trade_time"])
                )
                if next_time <= rule.latest_entry_time:
                    return i
            break

    return None


def detect_higher_low_breakout_entry(
    bars_d1: pl.DataFrame,
    prev_close: float,
    ts_code: str,
    rule: EntryRule | None = None,
) -> int | None:
    """higher_low_breakout: after 09:45, a higher low + close above VWAP + break prev high.

    Returns the index of the confirmation bar.
    """
    if bars_d1.height == 0:
        return None
    rule = rule or EntryRule()

    # Check gap-up cancel
    first_bar = bars_d1.row(0, named=True)
    first_open = float(first_bar["open"])
    if prev_close > 0:
        gap_pct = ((first_open - prev_close) / prev_close) * 100.0
        if gap_pct > rule.gap_up_pct:
            return None

    # Track the lowest low seen so far (the "front low")
    cum_amount = 0.0
    cum_volume = 0.0
    lowest_low = float("inf")
    prev_bar_high = 0.0
    found_higher_low = False

    for i in range(bars_d1.height):
        bar = bars_d1.row(i, named=True)
        bar_time = str(bar["trade_time"])
        time_part = bar_time[11:19] if len(bar_time) > 11 else ""

        low = float(bar["low"])
        high = float(bar["high"])
        close = float(bar["close"])
        volume = float(bar.get("volume", 0))
        amount = float(bar.get("amount", 0))

        cum_volume += volume
        cum_amount += amount

        if time_part < rule.min_confirm_time:
            lowest_low = min(lowest_low, low)
            prev_bar_high = high
            continue

        # No new entries after 14:30
        if time_part > rule.latest_entry_time:
            break

        # Check for higher low: current low > lowest_low seen before
        if low > lowest_low and not found_higher_low:
            found_higher_low = True

        vwap = cum_amount / cum_volume if cum_volume > 0 else close

        # Conditions: higher low found + close above VWAP + close breaks prev bar high
        if found_higher_low and close > vwap and prev_bar_high > 0 and close > prev_bar_high:
            if i + 1 < bars_d1.height:
                next_time = _bar_open_clock(
                    str(bars_d1.row(i + 1, named=True)["trade_time"])
                )
                if next_time <= rule.latest_entry_time:
                    return i
            break

        lowest_low = min(lowest_low, low)
        prev_bar_high = high

    return None


ENTRY_STRATEGIES = {
    "next_open": detect_next_open_entry,
    "vwap_reclaim": detect_vwap_reclaim_entry,
    "higher_low_breakout": detect_higher_low_breakout_entry,
}

HOLD_PERIODS = [1, 3, 5]


def _bar_open_clock(trade_time: str) -> str:
    """Return the actual open clock for a provider bar labelled by close time."""
    try:
        close_time = datetime.strptime(trade_time, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return ""
    return (close_time - timedelta(minutes=5)).strftime("%H:%M:%S")


# ---------------------------------------------------------------------------
# Statistics computation
# ---------------------------------------------------------------------------


def compute_strategy_stats(
    strategy_name: str,
    hold_days: int,
    trades: list[TradeResult],
    bootstrap_seed: int = 42,
    portfolio_capital: float = PORTFOLIO_CAPITAL,
    risk_budget_pct: float = RISK_BUDGET_PCT,
    position_cap_pct: float = POSITION_CAP_PCT,
    stop_loss_pct_cfg: float = STOP_LOSS_PCT,
) -> StrategyResult:
    """Compute summary statistics for a strategy's trades."""
    result = StrategyResult(strategy_name=strategy_name, hold_days=hold_days)
    result.events = len(trades)

    filled = [t for t in trades if t.filled]
    no_fill = [t for t in trades if not t.filled]
    result.filled_trades = len(filled)
    result.no_fill_count = len(no_fill)

    # No-fill reasons
    reasons: dict[str, int] = {}
    for t in no_fill:
        reasons[t.no_fill_reason] = reasons.get(t.no_fill_reason, 0) + 1
    result.no_fill_reasons = reasons
    result.trades = [t.to_dict() for t in trades]

    if not filled:
        return result

    net_returns = np.array([t.net_return for t in filled], dtype=float)
    if not np.all(np.isfinite(net_returns)) or np.any(net_returns <= -1.0):
        raise ValueError("filled trades contain invalid net returns")
    wins = net_returns[net_returns > 0]
    losses = net_returns[net_returns <= 0]

    result.win_rate = len(wins) / len(net_returns) if len(net_returns) > 0 else 0.0
    result.mean_net_return = float(np.mean(net_returns))
    result.median_net_return = float(np.median(net_returns))
    result.mean_win = float(np.mean(wins)) if len(wins) > 0 else 0.0
    result.mean_loss = float(np.mean(losses)) if len(losses) > 0 else 0.0
    result.profit_loss_ratio = (
        abs(result.mean_win / result.mean_loss) if result.mean_loss != 0 else float("inf")
    )

    gross_profit = float(np.sum(wins)) if len(wins) > 0 else 0.0
    gross_loss = abs(float(np.sum(losses))) if len(losses) > 0 else 0.0
    result.profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

    result.mean_mfe = float(np.mean([t.mfe for t in filled]))
    result.mean_mae = float(np.mean([t.mae for t in filled]))

    # 5% CVaR (expected shortfall)
    sorted_returns = np.sort(net_returns)
    cvar_idx = max(1, math.ceil(len(sorted_returns) * 0.05))
    result.cvar_5pct = float(np.mean(sorted_returns[:cvar_idx]))

    # Standard compounded-equity drawdown, including initial equity 1.0.
    equity = np.cumprod(1.0 + net_returns)
    equity_with_start = np.concatenate(([1.0], equity))
    running_max = np.maximum.accumulate(equity_with_start)
    drawdowns = (equity_with_start / running_max) - 1.0
    result.max_drawdown = float(np.min(drawdowns))

    # Use realized simulated P&L so lot rounding and idle cash are retained.
    pnl_per_10k = np.array(
        [trade.pnl_per_10000 for trade in filled],
        dtype=float,
    )
    if not np.all(np.isfinite(pnl_per_10k)):
        raise ValueError("filled trades contain invalid pnl_per_10000")
    result.expected_profit_per_10k = float(np.mean(pnl_per_10k))

    # Expected profit per 100k portfolio
    # Position size = min(risk_budget / stop_loss, position_cap) * portfolio
    risk_budget = portfolio_capital * (risk_budget_pct / 100.0)
    position_cap = portfolio_capital * (position_cap_pct / 100.0)
    position_size = min(risk_budget / (stop_loss_pct_cfg / 100.0), position_cap)
    result.expected_profit_per_100k = (
        result.expected_profit_per_10k * position_size / 10_000.0
    )

    # Return/CVaR ratio
    if result.cvar_5pct != 0:
        result.return_cvar_ratio = result.mean_net_return / abs(result.cvar_5pct)
    else:
        result.return_cvar_ratio = 0.0

    # Block bootstrap by decision_date
    result.bootstrap_ci_low, result.bootstrap_ci_high = _block_bootstrap(
        filled, net_returns, bootstrap_seed
    )

    return result


def _block_bootstrap(
    filled: list[TradeResult],
    net_returns: np.ndarray,
    seed: int,
    n_bootstrap: int = 1000,
) -> tuple[float, float]:
    """Block bootstrap by decision_date for 95% CI of mean net return."""
    if len(filled) < 5:
        mean = float(np.mean(net_returns))
        return mean, mean

    # Group by decision_date
    date_groups: dict[str, list[float]] = {}
    for t, r in zip(filled, net_returns, strict=False):
        date_groups.setdefault(t.decision_date, []).append(float(r))

    dates = sorted(date_groups)
    rng = np.random.default_rng(seed)
    bootstrap_means: list[float] = []

    for _ in range(n_bootstrap):
        sampled_dates = rng.choice(dates, size=len(dates), replace=True)
        sampled_returns: list[float] = []
        for d in sampled_dates:
            sampled_returns.extend(date_groups[d])
        if sampled_returns:
            bootstrap_means.append(float(np.mean(sampled_returns)))

    if not bootstrap_means:
        mean = float(np.mean(net_returns))
        return mean, mean

    bootstrap_means.sort()
    ci_low = bootstrap_means[int(0.025 * len(bootstrap_means))]
    ci_high = bootstrap_means[int(0.975 * len(bootstrap_means))]
    return ci_low, ci_high


# ---------------------------------------------------------------------------
# Strategy selection
# ---------------------------------------------------------------------------


def select_strategy(strategies: list[StrategyResult], min_trades: int = MIN_VAL_TRADES) -> tuple[str, str]:
    """Select the best strategy per pre-registered rules.

    Returns (selected_name, verdict).
    """
    # Filter: mean_net_return > 0 in both dev and val (handled by caller)
    # Here we just apply the ranking rules to a list of passing strategies
    passing = [s for s in strategies if s.mean_net_return > 0 and s.filled_trades >= min_trades]

    if not passing:
        return "", "no_viable_strategy"

    # Sort by expected_profit_per_100k descending
    passing.sort(key=lambda s: s.expected_profit_per_100k, reverse=True)

    # Tie-break: return_cvar_ratio
    best = passing[0]
    tied = [s for s in passing if abs(s.expected_profit_per_100k - best.expected_profit_per_100k) < 1e-8]
    if len(tied) > 1:
        tied.sort(key=lambda s: s.return_cvar_ratio, reverse=True)
        best = tied[0]
        # Further tie-break: max_drawdown (smaller is better)
        tied2 = [s for s in tied if abs(s.return_cvar_ratio - best.return_cvar_ratio) < 1e-8]
        if len(tied2) > 1:
            tied2.sort(key=lambda s: abs(s.max_drawdown))
            best = tied2[0]

    name = f"{best.strategy_name}_{best.hold_days}d"
    return name, "viable"


# ---------------------------------------------------------------------------
# Config hashing
# ---------------------------------------------------------------------------


def hash_config(config: dict[str, Any]) -> str:
    """Compute SHA-256 hash of the config dict."""
    serialized = json.dumps(config, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _finite_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{label} must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{label} must be a finite number")
    return number


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def load_rebound_config(config_path: Path | str) -> dict[str, Any]:
    """Load and validate rebound config."""
    def reject_constant(value: str) -> None:
        raise ValueError(f"config JSON must contain finite numbers: {value}")

    def parse_float(value: str) -> float:
        number = float(value)
        if not math.isfinite(number):
            raise ValueError(
                f"config JSON must contain finite numbers: {value}"
            )
        return number

    config = json.loads(
        Path(config_path).read_text("utf-8"),
        parse_constant=reject_constant,
        parse_float=parse_float,
    )
    if not isinstance(config, dict):
        raise ValueError("rebound config must be a JSON object")
    if config.get("version") != "1.1":
        raise ValueError("rebound config version must be 1.1")

    windows = config.get("data_window")
    if not isinstance(windows, dict):
        raise ValueError("data_window must be an object")
    parsed_windows: dict[str, tuple[str, str]] = {}
    for stage in ("development", "validation", "frozen"):
        window = windows.get(stage)
        if not isinstance(window, dict):
            raise ValueError(f"data_window.{stage} must be an object")
        start = window.get("start")
        end = window.get("end")
        if not isinstance(start, str) or not isinstance(end, str):
            raise ValueError(f"data_window.{stage} requires start and end")
        try:
            datetime.strptime(start, "%Y-%m-%d")
            if end != "latest":
                datetime.strptime(end, "%Y-%m-%d")
        except ValueError as error:
            raise ValueError(f"invalid data window for {stage}") from error
        if end != "latest" and start > end:
            raise ValueError(f"data_window.{stage} is reversed")
        parsed_windows[stage] = (start, end)
    if parsed_windows["development"][1] >= parsed_windows["validation"][0]:
        raise ValueError("development and validation windows must not overlap")
    if parsed_windows["validation"][1] >= parsed_windows["frozen"][0]:
        raise ValueError("validation and frozen windows must not overlap")

    event_thresholds = config.get("event_thresholds")
    required_event_fields = {
        "min_listing_days",
        "max_60d_position_pct",
        "min_60d_drawdown_pct",
        "max_5d_return_pct",
        "min_20d_avg_amount",
    }
    if not isinstance(event_thresholds, dict):
        raise ValueError("event_thresholds must be an object")
    missing_event_fields = sorted(required_event_fields - set(event_thresholds))
    if missing_event_fields:
        raise ValueError(
            f"event_thresholds missing fields: {missing_event_fields}"
        )
    min_listing_days = _positive_int(
        event_thresholds["min_listing_days"], "min_listing_days"
    )
    if min_listing_days < 60:
        raise ValueError("min_listing_days must be at least 60")
    position_pct = _finite_number(
        event_thresholds["max_60d_position_pct"], "max_60d_position_pct"
    )
    if not 0 <= position_pct <= 100:
        raise ValueError("max_60d_position_pct must be in [0, 100]")
    drawdown_pct = _finite_number(
        event_thresholds["min_60d_drawdown_pct"], "min_60d_drawdown_pct"
    )
    if not 0 <= drawdown_pct <= 100:
        raise ValueError("min_60d_drawdown_pct must be in [0, 100]")
    _finite_number(event_thresholds["max_5d_return_pct"], "max_5d_return_pct")
    if (
        _finite_number(
            event_thresholds["min_20d_avg_amount"], "min_20d_avg_amount"
        )
        <= 0
    ):
        raise ValueError("min_20d_avg_amount must be positive")

    configured = config.get("entry_strategies")
    if not isinstance(configured, list) or not configured:
        raise ValueError("entry_strategies must be a non-empty list")
    if not all(isinstance(entry, dict) for entry in configured):
        raise ValueError("each entry strategy must be an object")
    names = [entry.get("name") for entry in configured]
    if any(not isinstance(name, str) or not name for name in names):
        raise ValueError("each entry strategy requires a name")
    if len(names) != len(set(names)):
        raise ValueError("entry strategy names must be unique")
    unknown = sorted(set(names) - set(ENTRY_STRATEGIES))
    if unknown:
        raise ValueError(f"unknown entry strategies: {unknown}")
    if set(names) != set(ENTRY_STRATEGIES):
        raise ValueError("all three pre-registered entry strategies are required")
    for entry in configured:
        if entry["name"] == "next_open":
            continue
        for field_name in ("min_confirm_time", "latest_entry_time"):
            try:
                datetime.strptime(str(entry.get(field_name)), "%H:%M:%S")
            except ValueError as error:
                raise ValueError(
                    f"{entry['name']}.{field_name} must use HH:MM:SS"
                ) from error
        if str(entry["min_confirm_time"]) > str(entry["latest_entry_time"]):
            raise ValueError(
                f"{entry['name']} confirmation time is after latest entry"
            )
        if entry["name"] == "vwap_reclaim":
            _positive_int(
                entry.get("consecutive_bars"),
                "vwap_reclaim.consecutive_bars",
            )

    hold_periods = config.get("hold_periods")
    if not isinstance(hold_periods, list) or not hold_periods:
        raise ValueError("hold_periods must be a non-empty list")
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value <= 0
        for value in hold_periods
    ):
        raise ValueError("hold_periods must contain positive integers")
    if len(hold_periods) != len(set(hold_periods)):
        raise ValueError("hold_periods must be unique")
    if hold_periods != HOLD_PERIODS:
        raise ValueError(
            f"hold_periods must match the pre-registered periods {HOLD_PERIODS}"
        )

    cancel_conditions = config.get("cancel_conditions")
    if not isinstance(cancel_conditions, dict):
        raise ValueError("cancel_conditions must be an object")
    if _finite_number(
        cancel_conditions.get("gap_up_pct"), "gap_up_pct"
    ) < 0:
        raise ValueError("gap_up_pct must be non-negative")
    for field_name in (
        "no_confirmation",
        "limit_up",
        "suspended",
        "data_missing",
        "capacity_insufficient",
    ):
        if cancel_conditions.get(field_name) is not True:
            raise ValueError(f"cancel_conditions.{field_name} must be true")

    exit_rules = config.get("exit_rules")
    if not isinstance(exit_rules, dict):
        raise ValueError("exit_rules must be an object")
    stop_loss_pct = _finite_number(
        exit_rules.get("stop_loss_pct"), "stop_loss_pct"
    )
    if stop_loss_pct <= 0 or stop_loss_pct >= 100:
        raise ValueError("stop_loss_pct must be in (0, 100)")
    if exit_rules.get("stop_confirmation") != "5min_close":
        raise ValueError("stop_confirmation must be 5min_close")
    if exit_rules.get("stop_execution") != "next_bar_open":
        raise ValueError("stop_execution must be next_bar_open")
    if (
        exit_rules.get("hold_expire_execution")
        != "next_trading_day_first_bar"
    ):
        raise ValueError(
            "hold_expire_execution must be next_trading_day_first_bar"
        )

    execution = config.get("execution")
    if not isinstance(execution, dict):
        raise ValueError("execution must be an object")
    for field_name in ("fee_bps", "slippage_bps"):
        if _finite_number(
            execution.get(field_name), f"execution.{field_name}"
        ) < 0:
            raise ValueError(f"execution.{field_name} must be non-negative")
    if not 0 < _finite_number(
        execution.get("capacity_pct"), "execution.capacity_pct"
    ) <= 100:
        raise ValueError("execution.capacity_pct must be in (0, 100]")
    _positive_int(execution.get("lot_size"), "execution.lot_size")
    if execution.get("t_plus_1") is not True:
        raise ValueError("execution.t_plus_1 must be true")

    portfolio = config.get("portfolio")
    if not isinstance(portfolio, dict):
        raise ValueError("portfolio must be an object")
    for field_name in (
        "risk_budget_pct",
        "position_cap_pct",
        "portfolio_capital",
        "capital_per_trade",
    ):
        if _finite_number(
            portfolio.get(field_name), f"portfolio.{field_name}"
        ) <= 0:
            raise ValueError(f"portfolio.{field_name} must be positive")
    for field_name in ("risk_budget_pct", "position_cap_pct"):
        if float(portfolio[field_name]) > 100:
            raise ValueError(f"portfolio.{field_name} must not exceed 100")

    selection = config.get("selection_rules")
    if not isinstance(selection, dict):
        raise ValueError("selection_rules must be an object")
    for field_name in (
        "min_val_trades",
        "min_frozen_trading_days",
        "min_frozen_events",
    ):
        _positive_int(
            selection.get(field_name), f"selection_rules.{field_name}"
        )
    expected_order = {
        "primary_sort": "expected_profit_per_100k",
        "tiebreak_1": "return_cvar_ratio",
        "tiebreak_2": "max_drawdown_smaller",
    }
    for field_name, expected in expected_order.items():
        if selection.get(field_name) != expected:
            raise ValueError(f"selection_rules.{field_name} must be {expected}")

    if isinstance(config.get("bootstrap_seed"), bool) or not isinstance(
        config.get("bootstrap_seed"), int
    ):
        raise ValueError("bootstrap_seed must be an integer")
    if config.get("freq") != "5min":
        raise ValueError("rebound V1.1 only supports 5min data")
    return config


# ---------------------------------------------------------------------------
# Point-in-time universe filter
# ---------------------------------------------------------------------------


def filter_universe_point_in_time(
    universe_path: Path | str,
    as_of_date: str,
) -> set[str]:
    """Filter universe symbols by strategy_from/strategy_until for a given date.

    Args:
        universe_path: Path to universe.json.
        as_of_date: YYYY-MM-DD or YYYYMMDD date.

    Returns:
        Set of bare symbols active on as_of_date.
    """
    as_of = as_of_date.replace("-", "")
    membership = load_universe_membership(universe_path)
    return {
        symbol
        for symbol, (active_from, active_until) in membership.items()
        if active_from <= as_of <= active_until
    }


def load_universe_membership(
    universe_path: Path | str,
) -> dict[str, tuple[str, str]]:
    """Load point-in-time strategy membership for every eligible pool entry."""
    universe = json.loads(Path(universe_path).read_text("utf-8"))
    entries = universe.get("entries")
    if not isinstance(entries, list):
        raise ValueError("universe entries must be a list")
    membership: dict[str, tuple[str, str]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("each universe entry must be an object")
        if entry.get("pool_tier", "core") not in ("core", "watch"):
            continue
        symbol = str(entry.get("symbol") or "")
        if len(symbol) != 6 or not symbol.isdigit():
            raise ValueError(f"invalid universe symbol: {symbol!r}")
        if symbol in membership:
            raise ValueError(f"duplicate universe symbol: {symbol}")
        active_from = str(
            entry.get("strategy_from") or "0001-01-01"
        ).replace("-", "")
        active_until = str(
            entry.get("strategy_until") or "9999-12-31"
        ).replace("-", "")
        for label, value in (
            ("strategy_from", active_from),
            ("strategy_until", active_until),
        ):
            try:
                datetime.strptime(value, "%Y%m%d")
            except ValueError as error:
                raise ValueError(
                    f"invalid {label} for {symbol}: {value}"
                ) from error
        if active_from > active_until:
            raise ValueError(f"invalid strategy membership range for {symbol}")
        membership[symbol] = (active_from, active_until)
    if not membership:
        raise ValueError("universe has no core/watch members")
    return membership
