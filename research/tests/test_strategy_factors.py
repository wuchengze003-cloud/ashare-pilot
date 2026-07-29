from datetime import date, timedelta

import polars as pl

from ashare_research.strategy_factors import build_candidate_scores


def factor_frame() -> pl.DataFrame:
    rows = []
    start = date(2026, 1, 1)
    for day_index in range(6):
        day = start + timedelta(days=day_index)
        for symbol_index, symbol in enumerate(("sz000001", "sz000002", "sz000003")):
            rows.append(
                {
                    "date": day,
                    "symbol": symbol,
                    "is_universe_member": True,
                    "is_st": False,
                    "listing_age_bars": 200,
                    "amount": 100_000_000.0,
                    "next_trade_date": day + timedelta(days=1),
                    "next_raw_open": 10.0,
                    "next_raw_close": 10.1,
                    "momentum_5": -0.08 + symbol_index * 0.05,
                    "momentum_10": 0.01 + symbol_index * 0.02,
                    "momentum_20": -0.12 + symbol_index * 0.04,
                    "momentum_60": 0.03 + symbol_index * 0.04,
                    "volatility_20": 0.03 - symbol_index * 0.005,
                    "volatility_60": 0.04 - symbol_index * 0.005,
                    "ma_ratio_20": 0.01 + symbol_index * 0.01,
                    "market_breadth_5": 0.3,
                    "market_return_mean_20": -0.005,
                    "net_moneyflow_ratio": 0.01 + symbol_index * 0.01,
                    "large_order_ratio": 0.005 + symbol_index * 0.005,
                    "amount_ratio_20": 1.0 + symbol_index * 0.2,
                    "log_pe_ttm": 4.0 - symbol_index * 0.2,
                    "log_pb": 1.5 - symbol_index * 0.1,
                    "log_market_cap": 20.0 + symbol_index,
                    "position_60": 0.1 + symbol_index * 0.1,
                    "drawdown_60": -0.25 + symbol_index * 0.05,
                    "ret_1": 0.01 + symbol_index * 0.005,
                    "klow": 0.02,
                    "volume_ratio_5": 1.1 + symbol_index * 0.1,
                    "market_breadth": 0.5,
                    "label_return_5": 0.5,
                }
            )
    return pl.DataFrame(rows)


def test_anchor_scores_rank_low_vol_value_large_caps_higher():
    scored = build_candidate_scores(
        factor_frame(),
        "anchor-v1",
        {
            "low_vol_weight": 0.5,
            "value_weight": 0.3,
            "large_cap_weight": 0.2,
        },
    ).filter(pl.col("date") == date(2026, 1, 6))

    assert scored.sort("raw_score")["symbol"].to_list()[-1] == "sz000003"
    assert scored["eligible"].all()


def test_tide_fails_closed_when_moneyflow_is_missing():
    frame = factor_frame().with_columns(
        pl.lit(None).cast(pl.Float64).alias("net_moneyflow_ratio")
    )
    scored = build_candidate_scores(
        frame,
        "tide-v3",
        {
            "moneyflow_weight": 0.35,
            "large_order_weight": 0.25,
            "low_vol_weight": 0.35,
            "minimum_moneyflow_coverage": 0.8,
        },
    )

    assert not scored["eligible"].any()
    assert scored["raw_score"].null_count() == scored.height


def test_prism_requires_weak_market_and_breadth_recovery():
    scored = build_candidate_scores(
        factor_frame(),
        "prism-v3",
        {
            "reversal_weight": 0.45,
            "position_weight": 0.3,
            "wick_weight": 0.2,
            "maximum_breadth_5": 0.35,
            "minimum_breadth_rebound": 0.05,
        },
    ).filter(pl.col("date") == date(2026, 1, 6))

    assert scored["eligible"].all()
    falling = build_candidate_scores(
        factor_frame().with_columns(pl.lit(0.3).alias("market_breadth")),
        "prism-v3",
        {
            "reversal_weight": 0.45,
            "position_weight": 0.3,
            "wick_weight": 0.2,
            "maximum_breadth_5": 0.35,
            "minimum_breadth_rebound": 0.05,
        },
    )
    assert not falling["eligible"].any()


def test_strategy_scores_do_not_depend_on_future_labels():
    frame = factor_frame()
    params = {
        "low_vol_weight": 0.5,
        "value_weight": 0.3,
        "large_cap_weight": 0.2,
    }
    baseline = build_candidate_scores(frame, "anchor-v1", params)["raw_score"]
    changed = build_candidate_scores(
        frame.with_columns((pl.col("label_return_5") * -100).alias("label_return_5")),
        "anchor-v1",
        params,
    )["raw_score"]

    assert baseline.equals(changed, null_equal=True)


def test_cross_sectional_rank_ignores_non_members():
    frame = factor_frame()
    params = {
        "low_vol_weight": 0.5,
        "value_weight": 0.3,
        "large_cap_weight": 0.2,
    }
    baseline = (
        build_candidate_scores(frame, "anchor-v1", params)
        .filter(pl.col("date") == date(2026, 1, 6))
        .select("symbol", "raw_score")
        .sort("symbol")
    )
    outlier = (
        frame.filter(
            (pl.col("date") == date(2026, 1, 6))
            & (pl.col("symbol") == "sz000001")
        )
        .with_columns(
            pl.lit("sz999999").alias("symbol"),
            pl.lit(False).alias("is_universe_member"),
            pl.lit(-100.0).alias("volatility_20"),
            pl.lit(-100.0).alias("volatility_60"),
            pl.lit(-100.0).alias("log_pe_ttm"),
            pl.lit(-100.0).alias("log_pb"),
            pl.lit(100.0).alias("log_market_cap"),
        )
    )
    with_non_member = (
        build_candidate_scores(
            pl.concat([frame, outlier], how="vertical"),
            "anchor-v1",
            params,
        )
        .filter(
            (pl.col("date") == date(2026, 1, 6))
            & pl.col("is_universe_member")
        )
        .select("symbol", "raw_score")
        .sort("symbol")
    )

    assert baseline.equals(with_non_member)


def test_v3_daily_candidates_produce_scores():
    frame = factor_frame().with_columns(
        pl.lit(-0.002).alias("market_return_mean_20")
    )
    parameter_sets = {
        "harbor-v1": {
            "low_vol_weight": 0.4,
            "value_weight": 0.25,
            "moneyflow_weight": 0.15,
            "large_cap_weight": 0.2,
            "minimum_moneyflow_coverage": 0.5,
        },
        "surge-v1": {
            "low_vol_weight": 0.55,
            "momentum_weight": 0.15,
            "moneyflow_weight": 0.15,
            "large_cap_weight": 0.15,
            "minimum_moneyflow_coverage": 0.5,
            "minimum_market_breadth": 0.5,
            "minimum_market_return_mean_20": -0.003,
            "minimum_stock_momentum_60": 0,
        },
        "flow-v1": {
            "moneyflow_weight": 0.45,
            "large_order_weight": 0.15,
            "low_vol_weight": 0.4,
            "minimum_moneyflow_coverage": 0.5,
        },
    }

    for candidate_id, params in parameter_sets.items():
        scored = build_candidate_scores(frame, candidate_id, params)
        assert scored["candidate_id"].unique().to_list() == [candidate_id]
        assert scored["eligible"].any()
        assert scored["raw_score"].drop_nulls().len() > 0
