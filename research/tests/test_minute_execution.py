"""Tests for minute_execution module: T+1, limits, lots, fees, capacity."""

from __future__ import annotations

import polars as pl
import pytest

from ashare_research.minute_data import _valid_5min_times
from ashare_research.minute_execution import (
    ExecutionConfig,
    check_capacity,
    compute_entry_cost,
    compute_exit_cost,
    is_limit_down,
    is_limit_up,
    price_limit_fraction,
    round_lots,
    simulate_trade,
)


def _make_bars(ts_code: str, trade_date: str, n: int = 48, base_price: float = 10.0) -> pl.DataFrame:
    """Create synthetic 5min bars."""
    times = _valid_5min_times()[:n]
    rows = []
    for i, t in enumerate(times):
        price = base_price + i * 0.01
        rows.append({
            "ts_code": ts_code,
            "symbol": ts_code.split(".")[0],
            "trade_date": trade_date,
            "trade_time": f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:8]} {t}",
            "freq": "5min",
            "open": price,
            "high": price + 0.05,
            "low": price - 0.02,
            "close": price + 0.02,
            "volume": 1000000,
            "amount": 50000000.0,
            "source": "tushare_stk_mins",
            "fetched_at": "2025-01-01T00:00:00+00:00",
        })
    return pl.DataFrame(rows)


def _flat_bars(
    ts_code: str,
    trade_date: str,
    price: float,
    n: int = 48,
) -> pl.DataFrame:
    return _make_bars(ts_code, trade_date, n=n, base_price=price).with_columns(
        pl.lit(price).alias("open"),
        pl.lit(price).alias("high"),
        pl.lit(price).alias("low"),
        pl.lit(price).alias("close"),
    )


class TestPriceLimits:
    def test_main_board_10pct(self):
        assert price_limit_fraction("000001.SZ") == 0.10

    def test_chinext_20pct(self):
        assert price_limit_fraction("300308.SZ") == 0.20

    def test_star_20pct(self):
        assert price_limit_fraction("688256.SH") == 0.20

    def test_bse_30pct(self):
        assert price_limit_fraction("830799.BJ") == 0.30

    def test_limit_up_detection(self):
        assert is_limit_up(11.0, 11.0) is True
        assert is_limit_up(10.5, 11.0) is False

    def test_limit_down_detection(self):
        assert is_limit_down(9.0, 9.0) is True
        assert is_limit_down(9.5, 9.0) is False


class TestLotSize:
    def test_round_down(self):
        assert round_lots(150.0) == 100
        assert round_lots(99.0) == 0
        assert round_lots(250.0) == 200
        assert round_lots(1000.0) == 1000

    def test_custom_lot(self):
        assert round_lots(150.0, 50) == 150
        assert round_lots(149.0, 50) == 100


class TestFeesAndSlippage:
    def test_entry_cost_direction(self):
        """Entry cost should be higher than raw price (buying costs more)."""
        price = 10.0
        config = ExecutionConfig()
        cost = compute_entry_cost(price, config)
        assert cost > price

    def test_exit_cost_direction(self):
        """Exit cost should be lower than raw price (selling yields less)."""
        price = 10.0
        config = ExecutionConfig()
        cost = compute_exit_cost(price, config)
        assert cost < price

    def test_fee_magnitude(self):
        """10bps fee + 5bps slippage = 15bps total per side."""
        price = 10000.0
        config = ExecutionConfig(fee_bps=10.0, slippage_bps=5.0)
        entry = compute_entry_cost(price, config)
        assert abs(entry - 10015.0) < 0.01  # 15bps = 15 on 10000


class TestCapacity:
    def test_within_capacity(self):
        assert check_capacity(500000.0, 50000000.0, ExecutionConfig()) is True

    def test_exceeds_capacity(self):
        """Order > 1% of bar amount should be rejected."""
        assert check_capacity(600000.0, 50000000.0, ExecutionConfig()) is False

    def test_zero_bar_amount(self):
        assert check_capacity(100.0, 0.0, ExecutionConfig()) is False


class TestT1Rule:
    def test_cannot_sell_on_entry_day(self):
        """D+1 buy cannot D+1 sell - stop loss on entry day records t1_blocked."""
        config = ExecutionConfig()
        # Entry at bar[1] open = 10.01, stop at ~9.51 (5% below entry_with_cost)
        bars_d1 = _make_bars("000001.SZ", "20250102", base_price=10.0)
        # Make afternoon bars crash well below stop loss
        bars_d1 = bars_d1.with_columns(
            pl.when(pl.col("trade_time").str.slice(11, 8) >= "13:05:00")
            .then(pl.lit(9.0))
            .otherwise(pl.col("close"))
            .alias("close")
        )

        # Exit bars include entry day + next day
        bars_d2 = _make_bars("000001.SZ", "20250103", base_price=9.0)
        bars_exit = pl.concat([bars_d1, bars_d2])

        result = simulate_trade(
            event_id="test_t1",
            symbol="000001",
            ts_code="000001.SZ",
            decision_date="20250101",
            entry_bars=bars_d1,
            exit_bars=bars_exit,
            entry_signal_idx=0,
            hold_days=3,
            stop_loss_pct=0.05,
            config=config,
            capital_per_trade=10000.0,
            entry_up_limit=11.0,
            exit_down_limits={"20250103": 8.1},
            adj_factors={"20250102": 1.0, "20250103": 1.0},
            eligible_exit_date="20250103",
        )
        assert result.filled
        assert result.exit_time.startswith("2025-01-03")
        assert result.t1_blocked_stop is True

    def test_t1_blocked_stop_flag(self):
        """When stop triggers on D+1, t1_blocked_stop must be True."""
        config = ExecutionConfig()
        # Entry at 10.0, stop at 9.5 (5% below entry_with_cost ≈ 10.015)
        bars_d1 = _make_bars("000001.SZ", "20250102", n=48, base_price=10.0)
        # Make afternoon bars crash
        bars_d1 = bars_d1.with_columns(
            pl.when(pl.col("trade_time").str.slice(11, 8) >= "13:05:00")
            .then(pl.lit(9.4))
            .otherwise(pl.col("close"))
            .alias("close")
        )

        bars_d2 = _make_bars("000001.SZ", "20250103", base_price=9.3)
        bars_exit = pl.concat([bars_d1, bars_d2])

        result = simulate_trade(
            event_id="test_t1_flag",
            symbol="000001",
            ts_code="000001.SZ",
            decision_date="20250101",
            entry_bars=bars_d1,
            exit_bars=bars_exit,
            entry_signal_idx=0,
            hold_days=3,
            stop_loss_pct=0.05,
            config=config,
            capital_per_trade=10000.0,
            entry_up_limit=11.0,
            exit_down_limits={"20250103": 8.37},
            adj_factors={"20250102": 1.0, "20250103": 1.0},
            eligible_exit_date="20250103",
        )
        assert result.filled
        assert result.exit_reason == "stop_loss"
        assert result.t1_blocked_stop is True


class TestLimitUpCannotBuy:
    def test_limit_up_no_fill(self):
        """Cannot buy when opening at limit-up."""
        config = ExecutionConfig()
        bars_d1 = _make_bars("000001.SZ", "20250102", base_price=11.0)
        # prev_close = 10.0, limit up = 11.0
        bars_exit = _make_bars("000001.SZ", "20250103", base_price=11.0)

        result = simulate_trade(
            event_id="test_lu",
            symbol="000001",
            ts_code="000001.SZ",
            decision_date="20250101",
            entry_bars=bars_d1,
            exit_bars=bars_exit,
            entry_signal_idx=0,
            hold_days=1,
            stop_loss_pct=0.05,
            config=config,
            capital_per_trade=10000.0,
            entry_up_limit=11.0,
            exit_down_limits={"20250103": 9.9},
            adj_factors={"20250102": 1.0, "20250103": 1.0},
            eligible_exit_date="20250103",
        )
        assert not result.filled
        assert result.no_fill_reason == "no_fill_limit_up"

    def test_exact_st_limit_price_blocks_buy(self):
        """An ST 5% limit must not be inferred as a normal 10% board limit."""
        result = simulate_trade(
            event_id="test_st_limit",
            symbol="000001",
            ts_code="000001.SZ",
            decision_date="20250101",
            entry_bars=_flat_bars("000001.SZ", "20250102", 10.5),
            exit_bars=_flat_bars("000001.SZ", "20250103", 10.5),
            entry_signal_idx=-1,
            hold_days=1,
            stop_loss_pct=0.05,
            config=ExecutionConfig(),
            entry_up_limit=10.5,
            exit_down_limits={"20250103": 9.5},
            adj_factors={"20250102": 1.0, "20250103": 1.0},
            eligible_exit_date="20250103",
        )

        assert not result.filled
        assert result.no_fill_reason == "no_fill_limit_up"

    def test_missing_exact_entry_limit_fails_closed(self):
        result = simulate_trade(
            event_id="test_missing_entry_limit",
            symbol="000001",
            ts_code="000001.SZ",
            decision_date="20250101",
            entry_bars=_flat_bars("000001.SZ", "20250102", 10.0),
            exit_bars=_flat_bars("000001.SZ", "20250103", 10.0),
            entry_signal_idx=-1,
            hold_days=1,
            stop_loss_pct=0.05,
            config=ExecutionConfig(),
            adj_factors={"20250102": 1.0, "20250103": 1.0},
            eligible_exit_date="20250103",
        )

        assert not result.filled
        assert result.no_fill_reason == "no_fill_missing_limit_price"


class TestLimitDownCannotSell:
    def test_limit_down_exit_delayed(self):
        """Cannot sell at limit-down; exit is delayed."""
        config = ExecutionConfig()
        bars_d1 = _make_bars("000001.SZ", "20250102", base_price=10.0)
        # D+2 bars all at limit-down (9.0 = 10% below prev close of 10)
        bars_d2 = _make_bars("000001.SZ", "20250103", base_price=9.0)
        bars_d2 = bars_d2.with_columns(
            pl.lit(9.0).alias("open"),
            pl.lit(9.0).alias("close"),
            pl.lit(9.0).alias("high"),
            pl.lit(9.0).alias("low"),
        )
        # D+3 normal
        bars_d3 = _make_bars("000001.SZ", "20250104", base_price=9.1)
        bars_exit = pl.concat([bars_d1, bars_d2, bars_d3])

        result = simulate_trade(
            event_id="test_ld",
            symbol="000001",
            ts_code="000001.SZ",
            decision_date="20250101",
            entry_bars=bars_d1,
            exit_bars=bars_exit,
            entry_signal_idx=0,
            hold_days=1,
            stop_loss_pct=0.05,
            config=config,
            capital_per_trade=10000.0,
            entry_up_limit=11.0,
            exit_down_limits={"20250103": 9.0, "20250104": 8.1},
            adj_factors={
                "20250102": 1.0,
                "20250103": 1.0,
                "20250104": 1.0,
            },
            eligible_exit_date="20250103",
        )
        assert result.filled
        assert result.pending_exit_bars == 48
        assert result.exit_time.startswith("2025-01-04")

    def test_missing_exact_exit_limit_fails_closed(self):
        bars_d1 = _flat_bars("000001.SZ", "20250102", 10.0)
        bars_d2 = _flat_bars("000001.SZ", "20250103", 10.1)
        result = simulate_trade(
            event_id="test_missing_exit_limit",
            symbol="000001",
            ts_code="000001.SZ",
            decision_date="20250101",
            entry_bars=bars_d1,
            exit_bars=pl.concat([bars_d1, bars_d2]),
            entry_signal_idx=-1,
            hold_days=1,
            stop_loss_pct=0.05,
            config=ExecutionConfig(),
            entry_up_limit=11.0,
            exit_down_limits={},
            adj_factors={"20250102": 1.0, "20250103": 1.0},
            eligible_exit_date="20250103",
        )

        assert not result.filled
        assert result.no_fill_reason == "no_fill_missing_limit_price"


class TestSuspension:
    def test_suspended_no_execution(self):
        """Suspended day cannot execute."""
        config = ExecutionConfig()
        bars_d1 = _make_bars("000001.SZ", "20250102", base_price=10.0)
        bars_exit = _make_bars("000001.SZ", "20250103", base_price=10.0)

        result = simulate_trade(
            event_id="test_susp",
            symbol="000001",
            ts_code="000001.SZ",
            decision_date="20250101",
            entry_bars=bars_d1,
            exit_bars=bars_exit,
            entry_signal_idx=0,
            hold_days=1,
            stop_loss_pct=0.05,
            config=config,
            capital_per_trade=10000.0,
            entry_up_limit=11.0,
            exit_down_limits={"20250103": 9.0},
            adj_factors={"20250102": 1.0, "20250103": 1.0},
            suspended_dates={"20250102"},
        )
        assert not result.filled
        assert result.no_fill_reason == "no_fill_suspended"

    def test_suspended_exit_day_delays_to_next_tradable_day(self):
        bars_d1 = _flat_bars("000001.SZ", "20250102", 10.0)
        bars_d2 = _flat_bars("000001.SZ", "20250103", 10.0)
        bars_d3 = _flat_bars("000001.SZ", "20250106", 10.2)
        result = simulate_trade(
            event_id="test_suspended_exit",
            symbol="000001",
            ts_code="000001.SZ",
            decision_date="20250101",
            entry_bars=bars_d1,
            exit_bars=pl.concat([bars_d1, bars_d2, bars_d3]),
            entry_signal_idx=-1,
            hold_days=1,
            stop_loss_pct=0.05,
            config=ExecutionConfig(),
            entry_up_limit=11.0,
            exit_down_limits={"20250103": 9.0, "20250106": 9.0},
            suspended_dates={"20250103"},
            adj_factors={
                "20250102": 1.0,
                "20250103": 1.0,
                "20250106": 1.0,
            },
            eligible_exit_date="20250103",
        )

        assert result.filled
        assert result.pending_exit_bars == 48
        assert result.exit_time.startswith("2025-01-06")


class TestMissingBars:
    def test_no_entry_bars(self):
        """Missing entry bars → no_fill_data_missing."""
        config = ExecutionConfig()
        empty = pl.DataFrame()
        bars_exit = _make_bars("000001.SZ", "20250103", base_price=10.0)

        result = simulate_trade(
            event_id="test_missing",
            symbol="000001",
            ts_code="000001.SZ",
            decision_date="20250101",
            entry_bars=empty,
            exit_bars=bars_exit,
            entry_signal_idx=0,
            hold_days=1,
            stop_loss_pct=0.05,
            config=config,
        )
        assert not result.filled
        assert result.no_fill_reason == "no_fill_data_missing"

    def test_no_daily_fallback(self):
        """Missing bars must NOT use daily price as fallback."""
        config = ExecutionConfig()
        empty = pl.DataFrame()

        result = simulate_trade(
            event_id="test_no_fallback",
            symbol="000001",
            ts_code="000001.SZ",
            decision_date="20250101",
            entry_bars=empty,
            exit_bars=empty,
            entry_signal_idx=0,
            hold_days=1,
            stop_loss_pct=0.05,
            config=config,
        )
        assert not result.filled
        # Must be data_missing, not filled with daily price
        assert "data_missing" in result.no_fill_reason or "no_exit" in result.no_fill_reason


class TestCapacityReject:
    def test_capacity_exceeded_rejects(self):
        """Order exceeding 1% of bar amount is rejected."""
        config = ExecutionConfig(capacity_pct=1.0)
        bars_d1 = _make_bars("000001.SZ", "20250102", n=5, base_price=10.0)
        # Set very low amount
        bars_d1 = bars_d1.with_columns(pl.lit(100000.0).alias("amount"))
        bars_exit = _make_bars("000001.SZ", "20250103", base_price=10.0)

        result = simulate_trade(
            event_id="test_cap",
            symbol="000001",
            ts_code="000001.SZ",
            decision_date="20250101",
            entry_bars=bars_d1,
            exit_bars=bars_exit,
            entry_signal_idx=0,
            hold_days=1,
            stop_loss_pct=0.05,
            config=config,
            capital_per_trade=10000.0,  # 10000 > 1% of 100000 = 1000
            entry_up_limit=11.0,
            exit_down_limits={"20250103": 9.0},
            adj_factors={"20250102": 1.0, "20250103": 1.0},
            eligible_exit_date="20250103",
        )
        assert not result.filled
        assert result.no_fill_reason == "no_fill_capacity"


class TestExDateReturn:
    def test_ex_date_return_correct(self):
        """Cross-ex-date return uses adj_factor ratio, not raw price."""
        config = ExecutionConfig(fee_bps=0, slippage_bps=0)
        bars_d1 = _flat_bars("000001.SZ", "20250102", 10.0)
        bars_d2 = _flat_bars("000001.SZ", "20250103", 5.0)
        bars_exit = pl.concat([bars_d1, bars_d2])

        result = simulate_trade(
            event_id="test_exdate",
            symbol="000001",
            ts_code="000001.SZ",
            decision_date="20250101",
            entry_bars=bars_d1,
            exit_bars=bars_exit,
            entry_signal_idx=-1,
            hold_days=1,
            stop_loss_pct=0.05,
            config=config,
            capital_per_trade=10000.0,
            entry_up_limit=11.0,
            exit_down_limits={"20250103": 4.5},
            adj_factors={"20250102": 1.0, "20250103": 2.0},
            eligible_exit_date="20250103",
        )

        assert result.filled
        assert abs(result.gross_return) < 1e-12
        assert abs(result.net_return) < 1e-12
        assert abs(result.pnl_per_10000) < 1e-12

    def test_ex_date_raw_drop_does_not_false_trigger_stop(self):
        bars_d1 = _flat_bars("000001.SZ", "20250102", 10.0)
        bars_d2 = _flat_bars("000001.SZ", "20250103", 5.0)
        bars_d3 = _flat_bars("000001.SZ", "20250106", 5.1)
        result = simulate_trade(
            event_id="test_exdate_stop",
            symbol="000001",
            ts_code="000001.SZ",
            decision_date="20250101",
            entry_bars=bars_d1,
            exit_bars=pl.concat([bars_d1, bars_d2, bars_d3]),
            entry_signal_idx=-1,
            hold_days=2,
            stop_loss_pct=0.05,
            config=ExecutionConfig(fee_bps=0, slippage_bps=0),
            entry_up_limit=11.0,
            exit_down_limits={"20250106": 4.5},
            adj_factors={
                "20250102": 1.0,
                "20250103": 2.0,
                "20250106": 2.0,
            },
            eligible_exit_date="20250106",
        )

        assert result.filled
        assert result.exit_reason == "hold_2d_expire"
        assert result.mae >= 0

    def test_missing_adjustment_factor_fails_closed(self):
        bars_d1 = _flat_bars("000001.SZ", "20250102", 10.0)
        bars_d2 = _flat_bars("000001.SZ", "20250103", 10.1)
        result = simulate_trade(
            event_id="test_missing_factor",
            symbol="000001",
            ts_code="000001.SZ",
            decision_date="20250101",
            entry_bars=bars_d1,
            exit_bars=pl.concat([bars_d1, bars_d2]),
            entry_signal_idx=-1,
            hold_days=1,
            stop_loss_pct=0.05,
            config=ExecutionConfig(),
            entry_up_limit=11.0,
            exit_down_limits={"20250103": 9.0},
            adj_factors={"20250102": 1.0},
            eligible_exit_date="20250103",
        )

        assert not result.filled
        assert result.no_fill_reason == "no_fill_missing_adj_factor"


class TestBarCloseConfirmNextBarExec:
    def test_signal_at_close_exec_at_next_open(self):
        """Confirmation at bar close, execution at next bar open (no same-bar peek)."""
        config = ExecutionConfig(fee_bps=0, slippage_bps=0)
        bars_d1 = _make_bars("000001.SZ", "20250102", n=10, base_price=10.0)
        bars_exit = pl.concat([bars_d1, _make_bars("000001.SZ", "20250103", n=10, base_price=10.5)])

        # Signal at bar index 2 → execution at bar index 3's open
        result = simulate_trade(
            event_id="test_confirm",
            symbol="000001",
            ts_code="000001.SZ",
            decision_date="20250101",
            entry_bars=bars_d1,
            exit_bars=bars_exit,
            entry_signal_idx=2,
            hold_days=1,
            stop_loss_pct=0.05,
            config=config,
            capital_per_trade=10000.0,
            entry_up_limit=11.0,
            exit_down_limits={"20250103": 9.45},
            adj_factors={"20250102": 1.0, "20250103": 1.0},
            eligible_exit_date="20250103",
        )
        if result.filled:
            # Entry price should be bar[3]'s open, not bar[2]'s close
            expected_open = bars_d1.row(3, named=True)["open"]
            assert result.entry_price_raw == expected_open

    def test_confirmation_bar_is_not_replayed_after_entry(self):
        bars_d1 = _flat_bars("000001.SZ", "20250102", 10.0, n=10)
        bars_d1 = bars_d1.with_columns(
            pl.when(pl.int_range(pl.len()) == 2)
            .then(pl.lit(1.0))
            .otherwise(pl.col("close"))
            .alias("close"),
            pl.when(pl.int_range(pl.len()) == 2)
            .then(pl.lit(1.0))
            .otherwise(pl.col("low"))
            .alias("low"),
        )
        bars_d2 = _flat_bars("000001.SZ", "20250103", 10.0, n=10)

        result = simulate_trade(
            event_id="test_no_replay",
            symbol="000001",
            ts_code="000001.SZ",
            decision_date="20250101",
            entry_bars=bars_d1,
            exit_bars=pl.concat([bars_d1, bars_d2]),
            entry_signal_idx=2,
            hold_days=1,
            stop_loss_pct=0.05,
            config=ExecutionConfig(fee_bps=0, slippage_bps=0),
            entry_up_limit=11.0,
            exit_down_limits={"20250103": 9.0},
            adj_factors={"20250102": 1.0, "20250103": 1.0},
            eligible_exit_date="20250103",
        )

        assert result.filled
        assert result.entry_signal_time.endswith("09:45:00")
        assert result.entry_time.endswith("09:45:00")
        assert result.exit_time.endswith("09:30:00")
        assert result.exit_reason == "hold_1d_expire"


class TestExecutionPathMetrics:
    def test_mfe_and_mae_use_intrabar_high_and_low(self):
        bars_d1 = _flat_bars("000001.SZ", "20250102", 10.0, n=3)
        bars_d1 = bars_d1.with_columns(
            pl.when(pl.int_range(pl.len()) == 0)
            .then(pl.lit(12.0))
            .otherwise(pl.col("high"))
            .alias("high"),
            pl.when(pl.int_range(pl.len()) == 0)
            .then(pl.lit(8.0))
            .otherwise(pl.col("low"))
            .alias("low"),
        )
        bars_d2 = _flat_bars("000001.SZ", "20250103", 10.0, n=3)

        result = simulate_trade(
            event_id="test_excursions",
            symbol="000001",
            ts_code="000001.SZ",
            decision_date="20250101",
            entry_bars=bars_d1,
            exit_bars=pl.concat([bars_d1, bars_d2]),
            entry_signal_idx=-1,
            hold_days=1,
            stop_loss_pct=0.05,
            config=ExecutionConfig(fee_bps=0, slippage_bps=0),
            entry_up_limit=11.0,
            exit_down_limits={"20250103": 9.0},
            adj_factors={"20250102": 1.0, "20250103": 1.0},
            eligible_exit_date="20250103",
        )

        assert result.filled
        assert result.entry_signal_time == "2025-01-01 15:00:00"
        assert result.mfe == pytest.approx(0.2)
        assert result.mae == pytest.approx(-0.2)

    def test_early_eligible_date_cannot_bypass_t_plus_1(self):
        bars_d1 = _flat_bars("000001.SZ", "20250102", 10.0, n=3)
        bars_d2 = _flat_bars("000001.SZ", "20250103", 10.1, n=3)

        result = simulate_trade(
            event_id="test_early_exit",
            symbol="000001",
            ts_code="000001.SZ",
            decision_date="20250101",
            entry_bars=bars_d1,
            exit_bars=pl.concat([bars_d1, bars_d2]),
            entry_signal_idx=-1,
            hold_days=1,
            stop_loss_pct=0.05,
            config=ExecutionConfig(),
            entry_up_limit=11.0,
            exit_down_limits={"20250103": 9.0},
            adj_factors={"20250102": 1.0, "20250103": 1.0},
            eligible_exit_date="20250102",
        )

        assert result.filled
        assert result.exit_time.startswith("2025-01-03")

    def test_nan_entry_bar_fails_closed(self):
        bars_d1 = _flat_bars("000001.SZ", "20250102", 10.0, n=3)
        bars_d1 = bars_d1.with_columns(
            pl.when(pl.int_range(pl.len()) == 0)
            .then(pl.lit(float("nan")))
            .otherwise(pl.col("open"))
            .alias("open")
        )

        result = simulate_trade(
            event_id="test_nan_entry",
            symbol="000001",
            ts_code="000001.SZ",
            decision_date="20250101",
            entry_bars=bars_d1,
            exit_bars=_flat_bars("000001.SZ", "20250103", 10.0, n=3),
            entry_signal_idx=-1,
            hold_days=1,
            stop_loss_pct=0.05,
            config=ExecutionConfig(),
            entry_up_limit=11.0,
            adj_factors={"20250102": 1.0, "20250103": 1.0},
        )

        assert not result.filled
        assert result.no_fill_reason == "no_fill_invalid_bar"
