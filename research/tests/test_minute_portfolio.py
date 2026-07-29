from datetime import date, datetime, timedelta

import polars as pl
import pytest

from ashare_research.minute_portfolio import (
    MissingMinuteDataError,
    ParquetMinuteStore,
    collect_minute_requirements,
    prediction_bars_from_artifact,
    simulate_minute_portfolio,
)
from ashare_research.portfolio import PortfolioConfig


def _artifact(
    dates: list[date],
    predictions: list[float] | None = None,
) -> pl.DataFrame:
    predictions = predictions or [0.02] * len(dates)
    return pl.DataFrame(
        {
            "date": dates,
            "symbol": ["sz000001"] * len(dates),
            "raw_score": [0.8] * len(dates),
            "prediction": predictions,
            "close": [10.0] * len(dates),
            "next_trade_date": [
                value + timedelta(days=1) for value in dates
            ],
            "next_raw_open": [10.0] * len(dates),
            "next_raw_close": [10.0] * len(dates),
            "adj_factor": [1.0] * len(dates),
            "next_adj_factor": [1.0] * len(dates),
            "next_up_limit": [12.0] * len(dates),
            "next_down_limit": [8.0] * len(dates),
            "next_is_suspended": [False] * len(dates),
            "amount": [200_000_000.0] * len(dates),
            "volatility_20": [0.02] * len(dates),
            "theme": ["801010.SI:农林牧渔"] * len(dates),
        }
    )


def _bars(
    trade_date: date,
    closes: list[float],
    opens: list[float] | None = None,
) -> pl.DataFrame:
    values = list(closes)
    values.extend([values[-1]] * (48 - len(values)))
    open_values = list(opens or values)
    open_values.extend([open_values[-1]] * (48 - len(open_values)))
    morning = [
        datetime.combine(trade_date, datetime.min.time())
        + timedelta(hours=9, minutes=35 + 5 * index)
        for index in range(24)
    ]
    afternoon = [
        datetime.combine(trade_date, datetime.min.time())
        + timedelta(hours=13, minutes=5 + 5 * index)
        for index in range(24)
    ]
    times = morning + afternoon
    return pl.DataFrame(
        {
            "trade_date": [trade_date.strftime("%Y%m%d")] * 48,
            "trade_time": [
                value.strftime("%Y-%m-%d %H:%M:%S") for value in times
            ],
            "open": open_values,
            "high": [
                max(open_value, close_value) + 0.01
                for open_value, close_value in zip(
                    open_values,
                    values,
                    strict=True,
                )
            ],
            "low": [
                min(open_value, close_value) - 0.01
                for open_value, close_value in zip(
                    open_values,
                    values,
                    strict=True,
                )
            ],
            "close": values,
            "volume": [5_000_000.0] * 48,
            "amount": [50_000_000.0] * 48,
        }
    )


PARAMS = {
    "confirmation_bars": 2,
    "vwap_buffer_bps": 0,
    "maximum_chase_pct": 0.05,
    "hard_stop_pct": 0.04,
    "trailing_stop_pct": 0.08,
    "minimum_volume_ratio": 0,
    "rebound_from_low_pct": 0,
}


def test_minute_confirmation_fills_at_next_bar_open():
    decision = date(2026, 7, 20)
    execution_date = decision + timedelta(days=1)
    frame = _artifact([decision])
    bars = _bars(
        execution_date,
        closes=[10.0, 10.2, 10.3],
        opens=[10.0, 10.05, 10.25],
    )

    result = simulate_minute_portfolio(
        frame,
        "anchor-v1",
        PARAMS,
        lambda _symbol, _date: bars,
        PortfolioConfig(
            fee_bps=0,
            min_holding_bars=1,
            rebalance_threshold_pct=0,
        ),
    )

    buy = result.trades[0]
    assert buy.side == "buy"
    assert buy.execution_time == "2026-07-21 09:40:00"
    assert buy.price == pytest.approx(10.25)


def test_new_shares_cannot_be_stopped_until_next_trade_day():
    decisions = [date(2026, 7, 20), date(2026, 7, 21)]
    frame = _artifact(decisions)
    first_day = _bars(
        date(2026, 7, 21),
        closes=[10.0, 10.2, 10.3, 9.0],
        opens=[10.0, 10.05, 10.25, 9.1],
    )
    second_day = _bars(
        date(2026, 7, 22),
        closes=[9.0, 8.9, 8.8],
        opens=[9.0, 8.95, 8.85],
    )

    result = simulate_minute_portfolio(
        frame,
        "anchor-v1",
        PARAMS,
        lambda _symbol, value: (
            first_day if value == date(2026, 7, 21) else second_day
        ),
        PortfolioConfig(
            fee_bps=0,
            min_holding_bars=5,
            rebalance_threshold_pct=0,
        ),
    )

    sells = [trade for trade in result.trades if trade.side == "sell"]
    assert len(sells) == 1
    assert sells[0].trade_date == date(2026, 7, 22)
    assert sells[0].reason == "HARD_STOP_5MIN"
    assert sells[0].execution_time == "2026-07-22 09:35:00"


def test_minute_data_gap_fails_closed():
    decision = date(2026, 7, 20)

    with pytest.raises(MissingMinuteDataError) as error:
        simulate_minute_portfolio(
            _artifact([decision]),
            "anchor-v1",
            PARAMS,
            lambda _symbol, _date: pl.DataFrame(),
        )

    assert error.value.requirements[0].symbol == "sz000001"
    assert error.value.requirements[0].trade_date == date(2026, 7, 21)


def test_requirement_manifest_includes_minimum_hold_tail():
    decisions = [
        date(2026, 7, 20) + timedelta(days=index) for index in range(3)
    ]
    requirements = collect_minute_requirements(
        prediction_bars_from_artifact(_artifact(decisions)),
        PortfolioConfig(min_holding_bars=2),
    )

    assert {(item.symbol, item.trade_date) for item in requirements} == {
        ("sz000001", date(2026, 7, 21)),
        ("sz000001", date(2026, 7, 22)),
        ("sz000001", date(2026, 7, 23)),
    }


def test_limit_up_execution_is_not_filled():
    decision = date(2026, 7, 20)
    frame = _artifact([decision]).with_columns(
        pl.lit(10.25).alias("next_up_limit")
    )
    bars = _bars(
        date(2026, 7, 21),
        closes=[10.0, 10.2, 10.3],
        opens=[10.0, 10.05, 10.25],
    )

    result = simulate_minute_portfolio(
        frame,
        "anchor-v1",
        PARAMS,
        lambda _symbol, _date: bars,
        PortfolioConfig(
            fee_bps=0,
            min_holding_bars=1,
            rebalance_threshold_pct=0,
        ),
    )

    assert result.trades == ()


def test_parquet_minute_store_reads_one_month_once(tmp_path, monkeypatch):
    first = date(2026, 7, 20)
    second = date(2026, 7, 21)
    frame = pl.concat([_bars(first, [10.0]), _bars(second, [10.1])])
    frame = frame.with_columns(
        pl.lit("000001.SZ").alias("ts_code"),
        pl.lit("5min").alias("freq"),
    )
    target = (
        tmp_path
        / "raw"
        / "freq=5min"
        / "ts_code=000001.SZ"
        / "year=2026"
        / "month=07"
        / "part.parquet"
    )
    target.parent.mkdir(parents=True)
    frame.write_parquet(target)
    calls = 0
    original = pl.read_parquet

    def counted_read(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(pl, "read_parquet", counted_read)
    store = ParquetMinuteStore(tmp_path)

    assert store("sz000001", first).height == 48
    assert store("sz000001", second).height == 48
    assert calls == 1
