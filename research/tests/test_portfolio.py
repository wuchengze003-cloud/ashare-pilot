from datetime import date, timedelta

import pytest

from ashare_research.portfolio import PortfolioConfig, PredictionBar, simulate_portfolio


def bar(day, symbol, score, close=10, next_open=10, next_close=11, **kwargs):
    kwargs.setdefault("liquidity_amount_yuan", 100_000_000)
    kwargs.setdefault("volatility_20", 0.02)
    return PredictionBar(
        decision_date=day,
        trade_date=day + timedelta(days=1),
        symbol=symbol,
        score=score,
        close=close,
        next_open=next_open,
        next_close=next_close,
        **kwargs,
    )


def test_portfolio_buys_at_next_open_and_can_hold_cash():
    day = date(2026, 7, 1)
    result = simulate_portfolio(
        [bar(day, "300001", 0.02, next_open=10, next_close=11)],
        PortfolioConfig(start_cash=100_000, max_positions=1, fee_bps=0),
    )
    assert result.trades[0].trade_date == date(2026, 7, 2)
    assert result.trades[0].amount == pytest.approx(25_000)
    assert result.equity_curve[-1].equity == pytest.approx(102_500)

    cash = simulate_portfolio(
        [bar(day, "300001", -0.01)],
        PortfolioConfig(
            start_cash=100_000,
            max_positions=1,
            fee_bps=0,
            min_holding_bars=0,
        ),
    )
    assert cash.trades == ()
    assert cash.equity_curve[-1].cash == 100_000


def test_portfolio_does_not_concentrate_when_too_few_candidates_qualify():
    day = date(2026, 7, 1)
    result = simulate_portfolio(
        [bar(day, "300001", 0.02, next_open=10, next_close=10)],
        PortfolioConfig(start_cash=100_000, max_positions=4, fee_bps=0),
    )

    assert result.trades[0].amount == pytest.approx(25_000)
    assert result.equity_curve[-1].cash == pytest.approx(75_000)


def test_limit_lock_blocks_buy_and_sell_until_tradable():
    first = date(2026, 7, 1)
    second = date(2026, 7, 2)
    rows = [
        bar(first, "300001", 0.02, can_buy=False),
        bar(second, "300001", 0.02),
    ]
    result = simulate_portfolio(
        rows,
        PortfolioConfig(
            start_cash=100_000,
            max_positions=1,
            fee_bps=0,
            min_holding_bars=0,
        ),
    )
    assert len(result.trades) == 1
    assert result.trades[0].trade_date == date(2026, 7, 3)

    sell_rows = [
        bar(first, "300001", 0.02, next_close=10),
        bar(second, "300001", -0.02, next_close=10, can_sell=False),
        bar(date(2026, 7, 3), "300001", -0.02, next_close=10),
    ]
    sold = simulate_portfolio(
        sell_rows,
        PortfolioConfig(
            start_cash=100_000,
            max_positions=1,
            fee_bps=0,
            min_holding_bars=0,
        ),
    )
    assert [trade.side for trade in sold.trades] == ["buy", "sell"]
    assert sold.trades[-1].trade_date == date(2026, 7, 4)


def test_switch_buffer_avoids_replacing_a_nearly_equal_holding():
    first = date(2026, 7, 1)
    second = date(2026, 7, 2)
    result = simulate_portfolio(
        [
            bar(first, "300001", 0.020, next_close=10),
            bar(first, "300002", 0.010, next_close=10),
            bar(second, "300001", 0.019, next_close=10),
            bar(second, "300002", 0.020, next_close=10),
        ],
        PortfolioConfig(
            start_cash=100_000,
            max_positions=1,
            fee_bps=0,
            switch_buffer=0.002,
        ),
    )
    assert [trade.symbol for trade in result.trades] == ["300001"]


def test_calibrated_return_filters_and_raw_factor_ranks_independently():
    day = date(2026, 7, 1)
    result = simulate_portfolio(
        [
            bar(day, "300001", 0.001, ranking_score=0.1),
            bar(day, "300002", 0.001, ranking_score=0.9),
            bar(day, "300003", -0.001, ranking_score=1.0),
        ],
        PortfolioConfig(
            start_cash=100_000,
            max_positions=1,
            fee_bps=0,
            min_expected_return=0,
        ),
    )

    assert [trade.symbol for trade in result.trades] == ["300002"]


def test_production_costs_apply_minimum_commission_and_sell_stamp():
    first = date(2026, 7, 1)
    rows = [
        bar(first, "300001", 0.02, next_close=10),
        bar(first + timedelta(days=1), "300001", -0.02, next_close=10),
    ]
    result = simulate_portfolio(
        rows,
        PortfolioConfig(
            start_cash=10_000,
            max_positions=1,
            min_holding_bars=0,
            rebalance_threshold_pct=0,
        ),
    )

    buy, sell = result.trades
    assert buy.commission == pytest.approx(5)
    assert buy.stamp_duty == 0
    assert sell.commission == pytest.approx(5)
    assert sell.stamp_duty > 0
    assert buy.shares % 100 == 0
    assert result.equity_curve[-1].equity < 10_000


def test_liquidity_and_theme_caps_are_enforced():
    day = date(2026, 7, 1)
    rows = [
        bar(
            day,
            "300001",
            0.03,
            next_close=10,
            liquidity_amount_yuan=1_000_000,
            theme="AI芯片",
        ),
        bar(
            day,
            "300002",
            0.02,
            next_close=10,
            liquidity_amount_yuan=100_000_000,
            theme="AI芯片",
        ),
        bar(
            day,
            "300003",
            0.01,
            next_close=10,
            liquidity_amount_yuan=100_000_000,
            theme="AI芯片",
        ),
        bar(
            day,
            "300004",
            0.009,
            next_close=10,
            liquidity_amount_yuan=100_000_000,
            theme="液冷",
        ),
    ]
    result = simulate_portfolio(
        rows,
        PortfolioConfig(
            start_cash=1_000_000,
            max_positions=5,
            fee_bps=0,
            rebalance_threshold_pct=0,
        ),
    )

    by_symbol = {trade.symbol: trade.amount for trade in result.trades}
    assert by_symbol["300001"] <= 10_000
    assert (
        by_symbol["300001"] + by_symbol["300002"] + by_symbol.get("300003", 0)
        <= 400_000
    )
    assert all(amount <= 250_000 for amount in by_symbol.values())


def test_portfolio_marks_corporate_actions_with_total_return_factor():
    first = date(2026, 7, 1)
    factor_after_dividend = 10 / 9.5
    result = simulate_portfolio(
        [
            bar(
                first,
                "300001",
                0.02,
                next_open=10,
                next_close=10,
                adjustment_factor=1,
                next_adjustment_factor=1,
            ),
            bar(
                first + timedelta(days=1),
                "300001",
                0.02,
                close=9.5,
                next_open=9.5,
                next_close=9.5,
                adjustment_factor=factor_after_dividend,
                next_adjustment_factor=factor_after_dividend,
            ),
        ],
        PortfolioConfig(
            start_cash=100_000,
            max_positions=1,
            fee_bps=0,
            min_holding_bars=0,
        ),
    )

    assert result.equity_curve[-1].equity == pytest.approx(100_000)
