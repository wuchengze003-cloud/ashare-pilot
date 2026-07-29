"""Leakage-safe daily factor definitions for the three strategy candidates."""

from __future__ import annotations

from collections.abc import Callable

import polars as pl

MIN_DAILY_AMOUNT_YUAN = 50_000_000.0
MIN_LISTING_BARS = 120


def _require(frame: pl.DataFrame, columns: set[str]) -> None:
    missing = columns - set(frame.columns)
    if missing:
        raise ValueError(f"strategy factor frame missing columns: {sorted(missing)}")


def _percentile(column: str | pl.Expr) -> pl.Expr:
    expression = pl.col(column) if isinstance(column, str) else column
    member_value = pl.when(pl.col("is_universe_member")).then(expression)
    return member_value.rank(method="average").over(
        "date"
    ) / member_value.count().over("date")


def _base_eligible() -> pl.Expr:
    return (
        pl.col("is_universe_member")
        & (~pl.col("is_st"))
        & (pl.col("listing_age_bars") >= MIN_LISTING_BARS)
        & (pl.col("amount") >= MIN_DAILY_AMOUNT_YUAN)
        & pl.col("next_trade_date").is_not_null()
        & pl.col("next_raw_open").is_not_null()
        & pl.col("next_raw_close").is_not_null()
    )


def _with_moneyflow_features(
    frame: pl.DataFrame,
    window: int,
) -> pl.DataFrame:
    return frame.sort("symbol", "date").with_columns(
        pl.col("net_moneyflow_ratio")
        .rolling_mean(window, min_samples=1)
        .over("symbol")
        .alias("__moneyflow"),
        pl.col("large_order_ratio")
        .rolling_mean(window, min_samples=1)
        .over("symbol")
        .alias("__large_order"),
        pl.col("net_moneyflow_ratio")
        .is_not_null()
        .cast(pl.Int8)
        .rolling_mean(window, min_samples=1)
        .over("symbol")
        .alias("__moneyflow_coverage"),
    )


def _drop_moneyflow_features(frame: pl.DataFrame) -> pl.DataFrame:
    return frame.drop("__moneyflow", "__large_order", "__moneyflow_coverage")


def anchor_scores(frame: pl.DataFrame, params: dict[str, float]) -> pl.DataFrame:
    required = {
        "date",
        "symbol",
        "is_universe_member",
        "is_st",
        "listing_age_bars",
        "amount",
        "next_trade_date",
        "next_raw_open",
        "next_raw_close",
        "momentum_20",
        "volatility_20",
        "volatility_60",
        "log_pe_ttm",
        "log_pb",
        "log_market_cap",
    }
    _require(frame, required)
    low_vol_weight = params["low_vol_weight"]
    value_weight = params["value_weight"]
    large_cap_weight = params["large_cap_weight"]
    residual_weight = max(
        0.0,
        1.0 - low_vol_weight - value_weight - large_cap_weight,
    )
    eligible = (
        _base_eligible()
        & pl.col("volatility_20").is_not_null()
        & pl.col("volatility_60").is_not_null()
        & pl.col("log_pe_ttm").is_not_null()
        & pl.col("log_pb").is_not_null()
        & pl.col("log_market_cap").is_not_null()
    )
    low_volatility = (
        _percentile(-pl.col("volatility_20"))
        + _percentile(-pl.col("volatility_60"))
    ) / 2
    value = (
        _percentile(-pl.col("log_pe_ttm"))
        + _percentile(-pl.col("log_pb"))
    ) / 2
    raw_score = (
        low_volatility * low_vol_weight
        + value * value_weight
        + _percentile("log_market_cap") * large_cap_weight
        + _percentile(-pl.col("momentum_20")) * residual_weight
    )
    return frame.with_columns(
        pl.lit("anchor-v1").alias("candidate_id"),
        eligible.alias("eligible"),
        pl.when(eligible).then(raw_score).otherwise(None).alias("raw_score"),
    )


def tide_scores(frame: pl.DataFrame, params: dict[str, float]) -> pl.DataFrame:
    required = {
        "date",
        "symbol",
        "is_universe_member",
        "is_st",
        "listing_age_bars",
        "amount",
        "next_trade_date",
        "next_raw_open",
        "next_raw_close",
        "net_moneyflow_ratio",
        "large_order_ratio",
        "momentum_5",
        "volatility_20",
        "log_market_cap",
    }
    _require(frame, required)
    working = _with_moneyflow_features(frame, 5)
    moneyflow_weight = params["moneyflow_weight"]
    large_order_weight = params["large_order_weight"]
    low_vol_weight = params["low_vol_weight"]
    large_cap_weight = max(
        0.0,
        1.0 - moneyflow_weight - large_order_weight - low_vol_weight,
    )
    eligible = (
        _base_eligible()
        & (pl.col("__moneyflow_coverage") >= params["minimum_moneyflow_coverage"])
        & (pl.col("__moneyflow") > 0)
        & pl.col("volatility_20").is_not_null()
        & pl.col("log_market_cap").is_not_null()
        & (pl.col("momentum_5") < 0.095)
    )
    raw_score = (
        _percentile("__moneyflow") * moneyflow_weight
        + _percentile("__large_order") * large_order_weight
        + _percentile(-pl.col("volatility_20")) * low_vol_weight
        + _percentile("log_market_cap") * large_cap_weight
    )
    return _drop_moneyflow_features(
        working.with_columns(
            pl.lit("tide-v3").alias("candidate_id"),
            eligible.alias("eligible"),
            pl.when(eligible).then(raw_score).otherwise(None).alias("raw_score"),
        )
    )


def prism_scores(frame: pl.DataFrame, params: dict[str, float]) -> pl.DataFrame:
    required = {
        "date",
        "symbol",
        "is_universe_member",
        "is_st",
        "listing_age_bars",
        "amount",
        "next_trade_date",
        "next_raw_open",
        "next_raw_close",
        "momentum_20",
        "position_60",
        "ret_1",
        "klow",
        "market_breadth",
        "market_breadth_5",
        "market_return_mean_20",
        "large_order_ratio",
    }
    _require(frame, required)
    reversal_weight = params["reversal_weight"]
    position_weight = params["position_weight"]
    wick_weight = params["wick_weight"]
    flow_weight = max(
        0.0, 1.0 - reversal_weight - position_weight - wick_weight
    )
    weak_market = (
        (pl.col("market_return_mean_20") < -0.003)
        | (pl.col("market_breadth_5") < params["maximum_breadth_5"])
    )
    breadth_recovery = (
        pl.col("market_breadth")
        >= pl.col("market_breadth_5")
        + params["minimum_breadth_rebound"]
    )
    eligible = (
        _base_eligible()
        & weak_market
        & breadth_recovery
        & (pl.col("momentum_20") < 0)
        & (pl.col("ret_1") > 0)
    )
    raw_score = (
        _percentile(-pl.col("momentum_20")) * reversal_weight
        + _percentile(-pl.col("position_60")) * position_weight
        + _percentile(pl.col("klow").clip(0, 0.1)) * wick_weight
        + _percentile("large_order_ratio") * flow_weight
    )
    return frame.with_columns(
        pl.lit("prism-v3").alias("candidate_id"),
        eligible.alias("eligible"),
        pl.when(eligible).then(raw_score).otherwise(None).alias("raw_score"),
    )


def harbor_scores(frame: pl.DataFrame, params: dict[str, float]) -> pl.DataFrame:
    """Defensive value portfolio with persistent capital-flow confirmation."""

    required = {
        "date",
        "symbol",
        "is_universe_member",
        "is_st",
        "listing_age_bars",
        "amount",
        "next_trade_date",
        "next_raw_open",
        "next_raw_close",
        "net_moneyflow_ratio",
        "large_order_ratio",
        "volatility_20",
        "volatility_60",
        "log_pe_ttm",
        "log_pb",
        "log_market_cap",
    }
    _require(frame, required)
    working = _with_moneyflow_features(frame, 10)
    eligible = (
        _base_eligible()
        & (
            pl.col("__moneyflow_coverage")
            >= params["minimum_moneyflow_coverage"]
        )
        & pl.col("__moneyflow").is_not_null()
        & pl.col("volatility_20").is_not_null()
        & pl.col("volatility_60").is_not_null()
        & pl.col("log_pe_ttm").is_not_null()
        & pl.col("log_pb").is_not_null()
        & pl.col("log_market_cap").is_not_null()
    )
    low_volatility = (
        _percentile(-pl.col("volatility_20"))
        + _percentile(-pl.col("volatility_60"))
    ) / 2
    value = (
        _percentile(-pl.col("log_pe_ttm"))
        + _percentile(-pl.col("log_pb"))
    ) / 2
    raw_score = (
        low_volatility * params["low_vol_weight"]
        + value * params["value_weight"]
        + _percentile("__moneyflow") * params["moneyflow_weight"]
        + _percentile("log_market_cap") * params["large_cap_weight"]
    )
    return _drop_moneyflow_features(
        working.with_columns(
            pl.lit("harbor-v1").alias("candidate_id"),
            eligible.alias("eligible"),
            pl.when(eligible).then(raw_score).otherwise(None).alias("raw_score"),
        )
    )


def surge_scores(frame: pl.DataFrame, params: dict[str, float]) -> pl.DataFrame:
    """Risk-on participation strategy with explicit market-regime vetoes."""

    required = {
        "date",
        "symbol",
        "is_universe_member",
        "is_st",
        "listing_age_bars",
        "amount",
        "next_trade_date",
        "next_raw_open",
        "next_raw_close",
        "net_moneyflow_ratio",
        "large_order_ratio",
        "momentum_5",
        "momentum_60",
        "volatility_20",
        "volatility_60",
        "log_market_cap",
        "market_breadth",
        "market_return_mean_20",
    }
    _require(frame, required)
    working = _with_moneyflow_features(frame, 5)
    eligible = (
        _base_eligible()
        & (
            pl.col("__moneyflow_coverage")
            >= params["minimum_moneyflow_coverage"]
        )
        & (pl.col("__moneyflow") > 0)
        & (
            pl.col("market_breadth")
            >= params["minimum_market_breadth"]
        )
        & (
            pl.col("market_return_mean_20")
            >= params["minimum_market_return_mean_20"]
        )
        & (
            pl.col("momentum_60")
            >= params["minimum_stock_momentum_60"]
        )
        & (pl.col("momentum_5") < 0.095)
        & pl.col("volatility_20").is_not_null()
        & pl.col("volatility_60").is_not_null()
        & pl.col("log_market_cap").is_not_null()
    )
    low_volatility = (
        _percentile(-pl.col("volatility_20"))
        + _percentile(-pl.col("volatility_60"))
    ) / 2
    raw_score = (
        low_volatility * params["low_vol_weight"]
        + _percentile("momentum_60") * params["momentum_weight"]
        + _percentile("__moneyflow") * params["moneyflow_weight"]
        + _percentile("log_market_cap") * params["large_cap_weight"]
    )
    return _drop_moneyflow_features(
        working.with_columns(
            pl.lit("surge-v1").alias("candidate_id"),
            eligible.alias("eligible"),
            pl.when(eligible).then(raw_score).otherwise(None).alias("raw_score"),
        )
    )


def flow_scores(frame: pl.DataFrame, params: dict[str, float]) -> pl.DataFrame:
    """Concentrated capital-flow accumulation with a low-volatility ballast."""

    required = {
        "date",
        "symbol",
        "is_universe_member",
        "is_st",
        "listing_age_bars",
        "amount",
        "next_trade_date",
        "next_raw_open",
        "next_raw_close",
        "net_moneyflow_ratio",
        "large_order_ratio",
        "momentum_5",
        "volatility_20",
    }
    _require(frame, required)
    working = _with_moneyflow_features(frame, 5)
    eligible = (
        _base_eligible()
        & (
            pl.col("__moneyflow_coverage")
            >= params["minimum_moneyflow_coverage"]
        )
        & (pl.col("__moneyflow") > 0)
        & (pl.col("momentum_5") < 0.095)
        & pl.col("volatility_20").is_not_null()
    )
    raw_score = (
        _percentile("__moneyflow") * params["moneyflow_weight"]
        + _percentile("__large_order") * params["large_order_weight"]
        + _percentile(-pl.col("volatility_20")) * params["low_vol_weight"]
    )
    return _drop_moneyflow_features(
        working.with_columns(
            pl.lit("flow-v1").alias("candidate_id"),
            eligible.alias("eligible"),
            pl.when(eligible).then(raw_score).otherwise(None).alias("raw_score"),
        )
    )


SCORE_BUILDERS: dict[
    str, Callable[[pl.DataFrame, dict[str, float]], pl.DataFrame]
] = {
    "anchor-v1": anchor_scores,
    "tide-v3": tide_scores,
    "prism-v3": prism_scores,
    "harbor-v1": harbor_scores,
    "surge-v1": surge_scores,
    "flow-v1": flow_scores,
}


def build_candidate_scores(
    frame: pl.DataFrame,
    candidate_id: str,
    params: dict[str, float],
) -> pl.DataFrame:
    try:
        builder = SCORE_BUILDERS[candidate_id]
    except KeyError as error:
        raise ValueError(f"unsupported race candidate: {candidate_id}") from error
    return builder(frame, params)
