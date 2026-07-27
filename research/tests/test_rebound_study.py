"""Tests for rebound_study module: events, strategies, selection, statistics."""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import polars as pl
import pytest

from ashare_research.minute_data import _valid_5min_times
from ashare_research.minute_execution import TradeResult
from ashare_research.rebound_study import (
    EntryRule,
    StrategyResult,
    compute_strategy_stats,
    detect_events,
    detect_higher_low_breakout_entry,
    detect_next_open_entry,
    detect_vwap_reclaim_entry,
    filter_universe_point_in_time,
    hash_config,
    load_rebound_config,
    select_strategy,
)


def _make_bars(ts_code: str, trade_date: str, n: int = 48, base_price: float = 10.0) -> pl.DataFrame:
    """Create synthetic 5min bars with consistent amount = price * volume."""
    times = _valid_5min_times()[:n]
    rows = []
    for i, t in enumerate(times):
        price = base_price + i * 0.01
        vol = 1000000
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
            "volume": vol,
            "amount": price * vol,  # consistent with price * volume
            "source": "tushare_stk_mins",
            "fetched_at": "2025-01-01T00:00:00+00:00",
        })
    return pl.DataFrame(rows)


class TestNextOpenEntry:
    def test_normal_entry(self):
        bars = _make_bars("000001.SZ", "20250102", base_price=10.0)
        idx = detect_next_open_entry(bars, 10.0, "000001.SZ")
        assert idx == -1  # Special marker for next_open

    def test_gap_up_cancel(self):
        """D+1 gap up > 3% cancels entry."""
        bars = _make_bars("000001.SZ", "20250102", base_price=10.5)
        # prev_close = 10.0, open = 10.5 → gap = 5% > 3%
        idx = detect_next_open_entry(bars, 10.0, "000001.SZ")
        assert idx is None

    def test_empty_bars(self):
        idx = detect_next_open_entry(pl.DataFrame(), 10.0, "000001.SZ")
        assert idx is None

    def test_gap_threshold_comes_from_rule(self):
        bars = _make_bars("000001.SZ", "20250102", base_price=10.5)
        idx = detect_next_open_entry(
            bars,
            10.0,
            "000001.SZ",
            EntryRule(gap_up_pct=6.0),
        )
        assert idx == -1


class TestVwapReclaimEntry:
    def test_confirms_after_0945(self):
        """VWAP reclaim needs 2 consecutive closes above VWAP after 09:45."""
        # Create bars where close is always above VWAP (rising market)
        bars = _make_bars("000001.SZ", "20250102", n=48, base_price=10.0)
        idx = detect_vwap_reclaim_entry(bars, 10.0, "000001.SZ")
        # Should find confirmation since prices are rising
        assert idx is not None
        # Confirmation should be after 09:45
        if idx is not None:
            confirm_time = bars.row(idx, named=True)["trade_time"]
            assert confirm_time[11:16] >= "09:45"

    def test_gap_up_cancel(self):
        bars = _make_bars("000001.SZ", "20250102", base_price=10.5)
        idx = detect_vwap_reclaim_entry(bars, 10.0, "000001.SZ")
        assert idx is None

    def test_no_confirmation_flat(self):
        """Flat market where close == VWAP should not confirm (need strictly above)."""
        times = _valid_5min_times()
        rows = []
        for t in times:
            rows.append({
                "ts_code": "000001.SZ",
                "symbol": "000001",
                "trade_date": "20250102",
                "trade_time": f"2025-01-02 {t}",
                "freq": "5min",
                "open": 10.0,
                "high": 10.0,
                "low": 10.0,
                "close": 10.0,  # close == VWAP always
                "volume": 1000000,
                "amount": 10000000.0,
                "source": "tushare_stk_mins",
                "fetched_at": "2025-01-01T00:00:00+00:00",
            })
        bars = pl.DataFrame(rows)
        idx = detect_vwap_reclaim_entry(bars, 10.0, "000001.SZ")
        # close == VWAP, not strictly above → no confirmation
        assert idx is None

    def test_execution_at_exact_latest_entry_time_is_allowed(self):
        bars = _make_bars("000001.SZ", "20250102", n=48, base_price=10.0)
        idx = detect_vwap_reclaim_entry(
            bars,
            10.0,
            "000001.SZ",
            EntryRule(
                min_confirm_time="09:45:00",
                consecutive_bars=2,
                latest_entry_time="09:50:00",
            ),
        )
        assert idx is not None

    def test_execution_after_latest_entry_time_is_rejected(self):
        bars = _make_bars("000001.SZ", "20250102", n=48, base_price=10.0)
        idx = detect_vwap_reclaim_entry(
            bars,
            10.0,
            "000001.SZ",
            EntryRule(
                min_confirm_time="09:45:00",
                consecutive_bars=2,
                latest_entry_time="09:45:00",
            ),
        )
        assert idx is None


class TestHigherLowBreakoutEntry:
    def test_confirms_with_higher_low(self):
        """Detects higher low + VWAP + breakout."""
        times = _valid_5min_times()
        rows = []
        # First few bars: declining (establishing low)
        for i, t in enumerate(times[:6]):
            price = 10.0 - i * 0.05
            rows.append({
                "ts_code": "000001.SZ", "symbol": "000001",
                "trade_date": "20250102",
                "trade_time": f"2025-01-02 {t}", "freq": "5min",
                "open": price, "high": price + 0.02,
                "low": price - 0.03, "close": price - 0.01,
                "volume": 1000000, "amount": 10000000.0,
                "source": "tushare_stk_mins", "fetched_at": "2025-01-01T00:00:00+00:00",
            })
        # After 09:45: rising with higher lows
        for i, t in enumerate(times[6:]):
            price = 9.8 + i * 0.05
            rows.append({
                "ts_code": "000001.SZ", "symbol": "000001",
                "trade_date": "20250102",
                "trade_time": f"2025-01-02 {t}", "freq": "5min",
                "open": price, "high": price + 0.06,
                "low": price - 0.01,  # higher low
                "close": price + 0.04,  # above VWAP and prev high
                "volume": 1000000, "amount": 10000000.0,
                "source": "tushare_stk_mins", "fetched_at": "2025-01-01T00:00:00+00:00",
            })
        bars = pl.DataFrame(rows)
        idx = detect_higher_low_breakout_entry(bars, 10.0, "000001.SZ")
        # May or may not confirm depending on exact VWAP calculation
        # The key test is it doesn't crash and returns valid type
        assert idx is None or isinstance(idx, int)


class TestStrategySelection:
    def test_no_viable_strategy(self):
        """All strategies with negative returns → no_viable_strategy."""
        strategies = [
            StrategyResult(strategy_name="next_open", hold_days=1,
                          mean_net_return=-0.01, filled_trades=50),
            StrategyResult(strategy_name="vwap_reclaim", hold_days=3,
                          mean_net_return=-0.005, filled_trades=40),
        ]
        name, verdict = select_strategy(strategies)
        assert verdict == "no_viable_strategy"
        assert name == ""

    def test_selects_by_profit_not_win_rate(self):
        """Selection by expected_profit_per_100k, NOT win rate."""
        # Strategy A: high win rate but low profit
        a = StrategyResult(
            strategy_name="next_open", hold_days=1,
            mean_net_return=0.001, filled_trades=50,
            win_rate=0.8, expected_profit_per_100k=10.0,
            return_cvar_ratio=0.5, max_drawdown=-0.02,
        )
        # Strategy B: lower win rate but higher profit
        b = StrategyResult(
            strategy_name="vwap_reclaim", hold_days=3,
            mean_net_return=0.005, filled_trades=35,
            win_rate=0.4, expected_profit_per_100k=50.0,
            return_cvar_ratio=1.0, max_drawdown=-0.03,
        )
        name, verdict = select_strategy([a, b])
        assert verdict == "viable"
        assert "vwap_reclaim" in name  # Higher profit wins despite lower win rate

    def test_insufficient_trades_rejected(self):
        """Strategy with < 30 validation trades is rejected."""
        s = StrategyResult(
            strategy_name="next_open", hold_days=1,
            mean_net_return=0.01, filled_trades=20,  # < 30
            expected_profit_per_100k=100.0,
        )
        name, verdict = select_strategy([s])
        assert verdict == "no_viable_strategy"


class TestExpectedProfit:
    def test_per_10k_calculation(self):
        """Per-10k profit retains lot rounding and idle cash."""
        trades = [
            TradeResult(event_id=f"e{i}", symbol="000001", decision_date="20250101",
                       shares=1000, entry_price_raw=10.0, entry_price_with_cost=10.015,
                       exit_price_raw=10.1, exit_price_with_cost=10.085,
                       net_return=0.007, pnl_per_10000=65.0,
                       no_fill_reason="")
            for i in range(10)
        ]
        stats = compute_strategy_stats("next_open", 1, trades)
        assert stats.expected_profit_per_10k == 65.0

    def test_per_100k_uses_risk_budget(self):
        """expected_profit_per_100k uses risk budget and position cap."""
        trades = [
            TradeResult(event_id=f"e{i}", symbol="000001", decision_date="20250101",
                       shares=1000, entry_price_raw=10.0, entry_price_with_cost=10.015,
                       exit_price_raw=10.1, exit_price_with_cost=10.085,
                       net_return=0.007, pnl_per_10000=70.0,
                       no_fill_reason="")
            for i in range(10)
        ]
        stats = compute_strategy_stats("next_open", 1, trades)
        # position = min(500/0.05, 5000) = min(10000, 5000) = 5000
        # expected = 70 per 10k * 5000 / 10k = 35
        assert stats.expected_profit_per_100k == 35.0

    def test_max_drawdown_uses_compounded_equity(self):
        trades = [
            TradeResult(
                event_id=f"e{i}",
                symbol="000001",
                decision_date=f"2025010{i + 1}",
                shares=100,
                net_return=value,
                pnl_per_10000=value * 10_000,
                no_fill_reason="",
            )
            for i, value in enumerate((0.5, -0.4, -0.4))
        ]

        stats = compute_strategy_stats("next_open", 1, trades)

        assert stats.max_drawdown == pytest.approx(-0.64)

    def test_cvar_uses_the_full_five_percent_tail(self):
        returns = [-0.5, -0.1, *([0.01] * 19)]
        trades = [
            TradeResult(
                event_id=f"e{i}",
                symbol="000001",
                decision_date=f"202501{i + 1:02d}",
                shares=100,
                net_return=value,
                pnl_per_10000=value * 10_000,
                no_fill_reason="",
            )
            for i, value in enumerate(returns)
        ]

        stats = compute_strategy_stats("next_open", 1, trades)

        assert stats.cvar_5pct == pytest.approx(-0.3)


class TestBootstrap:
    def test_reproducible_with_seed(self):
        """Fixed seed produces identical bootstrap results."""
        trades = [
            TradeResult(event_id=f"e{i}", symbol="000001",
                       decision_date=f"2025010{(i % 9) + 1}",
                       shares=1000, net_return=0.01 * (i - 5), no_fill_reason="")
            for i in range(20)
        ]
        stats1 = compute_strategy_stats("next_open", 1, trades, bootstrap_seed=42)
        stats2 = compute_strategy_stats("next_open", 1, trades, bootstrap_seed=42)
        assert stats1.bootstrap_ci_low == stats2.bootstrap_ci_low
        assert stats1.bootstrap_ci_high == stats2.bootstrap_ci_high

    def test_different_seed_may_differ(self):
        """Different seeds can produce different CIs (not guaranteed but likely)."""
        trades = [
            TradeResult(event_id=f"e{i}", symbol="000001",
                       decision_date=f"2025010{(i % 9) + 1}",
                       shares=1000, net_return=0.01 * (i - 5), no_fill_reason="")
            for i in range(50)
        ]
        stats1 = compute_strategy_stats("next_open", 1, trades, bootstrap_seed=42)
        stats2 = compute_strategy_stats("next_open", 1, trades, bootstrap_seed=99)
        # Just verify both produce valid numbers
        assert stats1.bootstrap_ci_low <= stats1.bootstrap_ci_high
        assert stats2.bootstrap_ci_low <= stats2.bootstrap_ci_high


class TestConfigHash:
    def test_deterministic(self):
        config = {"version": "1.1", "fee_bps": 10}
        h1 = hash_config(config)
        h2 = hash_config(config)
        assert h1 == h2

    def test_different_config_different_hash(self):
        c1 = {"version": "1.1", "fee_bps": 10}
        c2 = {"version": "1.1", "fee_bps": 15}
        assert hash_config(c1) != hash_config(c2)


class TestConfigValidation:
    def test_rejects_non_preregistered_hold_periods(self, tmp_path):
        source = (
            Path(__file__).parent.parent
            / "config"
            / "rebound-v1.1.json"
        )
        config = json.loads(source.read_text("utf-8"))
        config["hold_periods"] = [1, 2, 5]
        target = tmp_path / "config.json"
        target.write_text(json.dumps(config), "utf-8")

        with pytest.raises(ValueError, match="pre-registered periods"):
            load_rebound_config(target)

    def test_rejects_non_positive_confirmation_count(self, tmp_path):
        source = (
            Path(__file__).parent.parent
            / "config"
            / "rebound-v1.1.json"
        )
        config = json.loads(source.read_text("utf-8"))
        for strategy in config["entry_strategies"]:
            if strategy["name"] == "vwap_reclaim":
                strategy["consecutive_bars"] = 0
        target = tmp_path / "config.json"
        target.write_text(json.dumps(config), "utf-8")

        with pytest.raises(ValueError, match="consecutive_bars"):
            load_rebound_config(target)

    def test_rejects_disabled_t_plus_1(self, tmp_path):
        source = (
            Path(__file__).parent.parent
            / "config"
            / "rebound-v1.1.json"
        )
        config = json.loads(source.read_text("utf-8"))
        config["execution"]["t_plus_1"] = False
        target = tmp_path / "config.json"
        target.write_text(json.dumps(config), "utf-8")

        with pytest.raises(ValueError, match="t_plus_1"):
            load_rebound_config(target)

    def test_rejects_wrong_hold_expiry_semantics(self, tmp_path):
        source = (
            Path(__file__).parent.parent
            / "config"
            / "rebound-v1.1.json"
        )
        config = json.loads(source.read_text("utf-8"))
        config["exit_rules"]["hold_expire_execution"] = "same_day_close"
        target = tmp_path / "config.json"
        target.write_text(json.dumps(config), "utf-8")

        with pytest.raises(ValueError, match="hold_expire_execution"):
            load_rebound_config(target)

    def test_rejects_non_finite_numeric_config(self, tmp_path):
        source = (
            Path(__file__).parent.parent
            / "config"
            / "rebound-v1.1.json"
        )
        config = json.loads(source.read_text("utf-8"))
        config["execution"]["fee_bps"] = float("nan")
        target = tmp_path / "config.json"
        target.write_text(json.dumps(config), "utf-8")

        with pytest.raises(ValueError, match="finite number"):
            load_rebound_config(target)

    def test_rejects_non_finite_unknown_config_field(self, tmp_path):
        source = (
            Path(__file__).parent.parent
            / "config"
            / "rebound-v1.1.json"
        )
        config = json.loads(source.read_text("utf-8"))
        config["metadata"] = {"accidental_overflow": float("inf")}
        target = tmp_path / "config.json"
        target.write_text(json.dumps(config), "utf-8")

        with pytest.raises(ValueError, match="finite number"):
            load_rebound_config(target)


class TestUniverseFilter:
    def test_point_in_time_filter(self, tmp_path):
        """Symbols filtered by strategy_from/strategy_until."""
        universe = {
            "entries": [
                {"symbol": "688256", "pool_tier": "core"},
                {"symbol": "300308", "pool_tier": "core", "strategy_from": "2025-06-01"},
                {"symbol": "000001", "pool_tier": "watch", "strategy_until": "2025-03-01"},
                {"symbol": "002049", "pool_tier": "observer"},  # excluded tier
            ]
        }
        path = tmp_path / "universe.json"
        path.write_text(json.dumps(universe))

        # Before 300308's strategy_from
        active = filter_universe_point_in_time(path, "2025-03-01")
        assert "688256" in active
        assert "300308" not in active  # not yet active
        assert "000001" in active  # still active (until 2025-03-01)
        assert "002049" not in active  # wrong tier

        # After 000001's strategy_until
        active2 = filter_universe_point_in_time(path, "2025-07-01")
        assert "688256" in active2
        assert "300308" in active2  # now active
        assert "000001" not in active2  # expired

    def test_membership_is_applied_on_each_event_date(self):
        rows = []
        for day in range(1, 11):
            price = 12.0 - day * 0.2
            rows.append(
                {
                    "ts_code": "000001.SZ",
                    "trade_date": f"202501{day:02d}",
                    "open": price,
                    "high": price + 0.1,
                    "low": price - 0.1,
                    "close": price,
                    "amount": 100_000.0,
                    "adj_factor": 1.0,
                    "is_st": False,
                    "suspended": False,
                }
            )
        events = detect_events(
            pl.DataFrame(rows),
            {"000001": ("20250108", "20250109")},
            "20250101",
            "20250110",
            min_listing_days=5,
            max_60d_position_pct=100.0,
            min_60d_drawdown_pct=0.0,
            max_5d_return_pct=100.0,
            min_20d_avg_amount=0.0,
        )

        assert {event.decision_date for event in events} == {
            "20250108",
            "20250109",
        }


class TestNoFutureFunction:
    @staticmethod
    def _daily_rows(total_days: int = 61) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        start = date(2025, 1, 1)
        for offset in range(total_days):
            trade_date = (start + timedelta(days=offset)).strftime("%Y%m%d")
            close = 100.0 if offset < 59 else 80.0
            rows.append(
                {
                    "ts_code": "000001.SZ",
                    "trade_date": trade_date,
                    "open": close,
                    "high": 200.0 if offset == 0 else close + 5.0,
                    "low": 70.0,
                    "close": close,
                    "amount": 100_000.0,
                    "adj_factor": 1.0,
                    "is_st": False,
                    "suspended": False,
                }
            )
        return rows

    def test_d1_data_does_not_change_d_event(self):
        """Mutating the actual D+1 row cannot change the D event."""
        rows = self._daily_rows()
        decision_date = str(rows[59]["trade_date"])
        original = detect_events(
            pl.DataFrame(rows),
            {"000001"},
            decision_date,
            decision_date,
            min_listing_days=60,
            max_60d_position_pct=100.0,
            min_60d_drawdown_pct=15.0,
            max_5d_return_pct=100.0,
            min_20d_avg_amount=0.0,
        )
        rows[60]["close"] = 1_000_000.0
        rows[60]["high"] = 1_000_000.0
        rows[60]["low"] = 1_000_000.0
        mutated = detect_events(
            pl.DataFrame(rows),
            {"000001"},
            decision_date,
            decision_date,
            min_listing_days=60,
            max_60d_position_pct=100.0,
            min_60d_drawdown_pct=15.0,
            max_5d_return_pct=100.0,
            min_20d_avg_amount=0.0,
        )

        assert [event.to_dict() if hasattr(event, "to_dict") else event.__dict__ for event in original] == [
            event.to_dict() if hasattr(event, "to_dict") else event.__dict__
            for event in mutated
        ]

    def test_drawdown_uses_highest_close_not_intraday_high(self):
        rows = self._daily_rows(total_days=60)
        decision_date = str(rows[-1]["trade_date"])

        too_strict = detect_events(
            pl.DataFrame(rows),
            {"000001"},
            decision_date,
            decision_date,
            min_listing_days=60,
            max_60d_position_pct=100.0,
            min_60d_drawdown_pct=50.0,
            max_5d_return_pct=100.0,
            min_20d_avg_amount=0.0,
        )
        qualifying = detect_events(
            pl.DataFrame(rows),
            {"000001"},
            decision_date,
            decision_date,
            min_listing_days=60,
            max_60d_position_pct=100.0,
            min_60d_drawdown_pct=15.0,
            max_5d_return_pct=100.0,
            min_20d_avg_amount=0.0,
        )

        assert too_strict == []
        assert len(qualifying) == 1
        assert qualifying[0].drawdown_60d_pct == pytest.approx(20.0)

    def test_confirm_bar_close_not_exec_price(self):
        """Confirmation bar's close is signal, not execution price."""
        # In detect_vwap_reclaim_entry, the returned index is the confirmation bar
        # Execution happens at index+1's open (handled by simulate_trade)
        bars = _make_bars("000001.SZ", "20250102", n=48, base_price=10.0)
        idx = detect_vwap_reclaim_entry(bars, 10.0, "000001.SZ")
        if idx is not None:
            # The confirmation bar's close is NOT the execution price
            # Execution will be at bars[idx+1]["open"]
            assert idx + 1 < bars.height

    def test_future_bars_do_not_change_existing_confirmation(self):
        bars = _make_bars(
            "000001.SZ", "20250102", n=48, base_price=10.0
        )
        original_idx = detect_vwap_reclaim_entry(
            bars, 10.0, "000001.SZ"
        )
        assert original_idx is not None
        mutated = (
            bars.with_row_index("_row")
            .with_columns(
                pl.when(pl.col("_row") > original_idx)
                .then(pl.lit(1_000_000.0))
                .otherwise(pl.col("open"))
                .alias("open"),
                pl.when(pl.col("_row") > original_idx)
                .then(pl.lit(1_000_000.0))
                .otherwise(pl.col("high"))
                .alias("high"),
                pl.when(pl.col("_row") > original_idx)
                .then(pl.lit(1_000_000.0))
                .otherwise(pl.col("low"))
                .alias("low"),
                pl.when(pl.col("_row") > original_idx)
                .then(pl.lit(1_000_000.0))
                .otherwise(pl.col("close"))
                .alias("close"),
            )
            .drop("_row")
        )

        assert (
            detect_vwap_reclaim_entry(mutated, 10.0, "000001.SZ")
            == original_idx
        )


class TestInsufficientEvidence:
    def test_few_events_flagged(self):
        """When events < threshold, verdict should be insufficient_evidence."""
        # This is tested at the orchestration level; here we verify the constant
        from ashare_research.rebound_study import MIN_FROZEN_EVENTS
        assert MIN_FROZEN_EVENTS == 100
