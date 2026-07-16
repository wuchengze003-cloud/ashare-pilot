"""Polars feature panel with execution-aligned multi-horizon labels."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import polars as pl

from .contracts import HORIZONS

FEATURE_VERSION = "ashare-core-v3"
PRICE_WINDOWS = (1, 3, 5, 10, 20, 60)
ROLLING_WINDOWS = (5, 10, 20, 60)


@dataclass(frozen=True)
class FeatureBuildResult:
    output_path: Path
    rows: int
    symbols: int
    start_date: str
    end_date: str
    feature_names: tuple[str, ...]


def canonical_symbol(ts_code: str) -> str:
    code, exchange = ts_code.split(".", 1)
    prefix = {"SH": "sh", "SZ": "sz", "BJ": "bj"}.get(exchange.upper())
    if not prefix:
        raise ValueError(f"unsupported exchange: {ts_code}")
    return f"{prefix}{code}"


def _scan_optional(path: Path, columns: list[str]) -> pl.LazyFrame | None:
    if not path.exists() or not any(path.rglob("*.parquet")):
        return None
    scan = pl.scan_parquet(str(path / "**" / "*.parquet"), missing_columns="insert")
    available = set(scan.collect_schema().names())
    selected = [column for column in columns if column in available]
    return scan.select(selected) if selected else None


def _ensure_columns(frame: pl.LazyFrame, defaults: dict[str, float | None]) -> pl.LazyFrame:
    available = set(frame.collect_schema().names())
    missing = [
        pl.lit(value).alias(name) for name, value in defaults.items() if name not in available
    ]
    return frame.with_columns(missing) if missing else frame


def build_feature_panel(
    data_root: Path | str,
    output_path: Path | str,
    round_trip_fee_bps: float = 10,
    as_of_date: str | None = None,
) -> FeatureBuildResult:
    data_root = Path(data_root)
    output_path = Path(output_path)
    daily_path = data_root / "raw" / "daily"
    if not daily_path.exists():
        raise FileNotFoundError(f"daily parquet not found: {daily_path}")

    daily = pl.scan_parquet(str(daily_path / "**" / "*.parquet"))
    daily = daily.select("ts_code", "trade_date", "open", "high", "low", "close", "vol", "amount")
    if as_of_date is not None:
        daily = daily.filter(pl.col("trade_date").cast(pl.String) <= as_of_date.replace("-", ""))
    adjustment = _scan_optional(
        data_root / "raw" / "adj_factor", ["ts_code", "trade_date", "adj_factor"]
    )
    if adjustment is None:
        raise FileNotFoundError("adj_factor parquet is required")
    panel = daily.join(adjustment, on=["ts_code", "trade_date"], how="left")

    basic = _scan_optional(
        data_root / "raw" / "daily_basic",
        ["ts_code", "trade_date", "turnover_rate", "pe_ttm", "pb", "total_mv"],
    )
    if basic is not None:
        panel = panel.join(basic, on=["ts_code", "trade_date"], how="left")
    moneyflow = _scan_optional(
        data_root / "raw" / "moneyflow",
        ["ts_code", "trade_date", "net_mf_amount", "buy_lg_amount", "sell_lg_amount"],
    )
    if moneyflow is not None:
        panel = panel.join(moneyflow, on=["ts_code", "trade_date"], how="left")
    limits = _scan_optional(
        data_root / "raw" / "stk_limit",
        ["ts_code", "trade_date", "up_limit", "down_limit"],
    )
    if limits is None:
        raise FileNotFoundError("stk_limit parquet is required for executable labels")
    panel = panel.join(limits, on=["ts_code", "trade_date"], how="left")
    suspended = _scan_optional(
        data_root / "raw" / "suspend_d",
        ["ts_code", "trade_date", "suspend_type"],
    )
    if suspended is not None:
        suspended = (
            suspended.select("ts_code", "trade_date")
            .unique()
            .with_columns(pl.lit(True).alias("is_suspended"))
        )
        panel = panel.join(suspended, on=["ts_code", "trade_date"], how="left")

    panel = _ensure_columns(
        panel,
        {
            "turnover_rate": None,
            "pe_ttm": None,
            "pb": None,
            "total_mv": None,
            "net_mf_amount": 0.0,
            "buy_lg_amount": 0.0,
            "sell_lg_amount": 0.0,
            "is_suspended": False,
        },
    )
    panel = panel.with_columns(
        pl.col("trade_date").cast(pl.String).str.strptime(pl.Date, "%Y%m%d").alias("date"),
        pl.col("ts_code").map_elements(canonical_symbol, return_dtype=pl.String).alias("symbol"),
        pl.col("adj_factor").fill_null(1.0),
        pl.col("is_suspended").fill_null(False),
    ).sort("symbol", "date")
    namechange_path = data_root / "reference" / "namechange.parquet"
    if namechange_path.exists():
        namechange = pl.scan_parquet(namechange_path)
        available = set(namechange.collect_schema().names())
        required_namechange = {"ts_code", "name", "start_date", "end_date"}
        if required_namechange <= available:
            st_periods = (
                namechange.select(*sorted(required_namechange))
                .filter(pl.col("name").cast(pl.String).str.contains(r"(?i)ST"))
                .with_columns(
                    pl.col("start_date")
                    .cast(pl.String)
                    .str.strptime(pl.Date, "%Y%m%d", strict=False)
                    .alias("st_start"),
                    pl.col("end_date")
                    .cast(pl.String)
                    .fill_null("29991231")
                    .str.strptime(pl.Date, "%Y%m%d", strict=False)
                    .fill_null(pl.date(2999, 12, 31))
                    .alias("st_end"),
                )
                .drop_nulls("st_start")
                .select("ts_code", "st_start", "st_end")
                .sort("ts_code", "st_start")
            )
            panel = panel.sort("ts_code", "date").join_asof(
                st_periods,
                left_on="date",
                right_on="st_start",
                by="ts_code",
                strategy="backward",
                check_sortedness=False,
            )
            panel = panel.with_columns(
                (pl.col("st_start").is_not_null() & (pl.col("date") <= pl.col("st_end")))
                .fill_null(False)
                .alias("is_st")
            )
        else:
            panel = panel.with_columns(pl.lit(False).alias("is_st"))
    else:
        panel = panel.with_columns(pl.lit(False).alias("is_st"))
    panel = panel.with_columns(
        pl.col("date").rank("ordinal").over("symbol").alias("listing_age_bars")
    )
    panel = panel.with_columns(
        (pl.col(field) * pl.col("adj_factor")).alias(f"adj_{field}")
        for field in ("open", "high", "low", "close")
    )
    panel = panel.with_columns(
        pl.col("adj_close").shift(1).over("symbol").alias("prev_close"),
        pl.col("vol").cast(pl.Float64).alias("volume"),
        (
            (~pl.col("is_suspended"))
            & (~pl.col("is_st"))
            & pl.col("open").is_not_null()
            & pl.col("up_limit").is_not_null()
            & (pl.col("open") < pl.col("up_limit") - 1e-8)
        ).alias("can_buy_open"),
        (
            (~pl.col("is_suspended"))
            & pl.col("open").is_not_null()
            & pl.col("down_limit").is_not_null()
            & (pl.col("open") > pl.col("down_limit") + 1e-8)
        ).alias("can_sell_open"),
    )
    panel = panel.with_columns(
        (pl.col("adj_close") / pl.col("prev_close") - 1).alias("ret_1"),
        pl.max_horizontal(
            pl.col("adj_high") - pl.col("adj_low"),
            (pl.col("adj_high") - pl.col("prev_close")).abs(),
            (pl.col("adj_low") - pl.col("prev_close")).abs(),
        ).alias("true_range"),
        ((pl.col("adj_close") - pl.col("adj_open")) / pl.col("adj_open")).alias("kmid"),
        ((pl.col("adj_high") - pl.col("adj_low")) / pl.col("adj_open")).alias("klen"),
        (
            (pl.col("adj_high") - pl.max_horizontal("adj_open", "adj_close")) / pl.col("adj_open")
        ).alias("kup"),
        (
            (pl.min_horizontal("adj_open", "adj_close") - pl.col("adj_low")) / pl.col("adj_open")
        ).alias("klow"),
    )

    feature_names = ["kmid", "klen", "kup", "klow", "ret_1"]
    expressions: list[pl.Expr] = []
    for window in PRICE_WINDOWS:
        name = f"momentum_{window}"
        expressions.append(
            (pl.col("adj_close") / pl.col("adj_close").shift(window).over("symbol") - 1).alias(name)
        )
        feature_names.append(name)
    for window in ROLLING_WINDOWS:
        definitions = {
            f"ma_ratio_{window}": pl.col("adj_close")
            / pl.col("adj_close").rolling_mean(window).over("symbol")
            - 1,
            f"volatility_{window}": pl.col("ret_1").rolling_std(window).over("symbol"),
            f"volume_ratio_{window}": pl.col("volume")
            / pl.col("volume").rolling_mean(window).over("symbol"),
            f"amount_ratio_{window}": pl.col("amount")
            / pl.col("amount").rolling_mean(window).over("symbol"),
        }
        for name, expression in definitions.items():
            expressions.append(expression.alias(name))
            feature_names.append(name)
    expressions.extend(
        [
            (pl.col("true_range").rolling_mean(14).over("symbol") / pl.col("adj_close")).alias(
                "atr_14_pct"
            ),
            (
                pl.col("turnover_rate") / pl.col("turnover_rate").rolling_mean(20).over("symbol")
            ).alias("turnover_ratio_20"),
            (pl.col("net_mf_amount") / (pl.col("amount").abs() + 1)).alias("net_moneyflow_ratio"),
            (
                (pl.col("buy_lg_amount") - pl.col("sell_lg_amount")) / (pl.col("amount").abs() + 1)
            ).alias("large_order_ratio"),
            pl.col("pe_ttm").log1p().alias("log_pe_ttm"),
            pl.col("pb").log1p().alias("log_pb"),
            pl.col("total_mv").log1p().alias("log_market_cap"),
        ]
    )
    feature_names.extend(
        [
            "atr_14_pct",
            "turnover_ratio_20",
            "net_moneyflow_ratio",
            "large_order_ratio",
            "log_pe_ttm",
            "log_pb",
            "log_market_cap",
        ]
    )
    panel = panel.with_columns(expressions)
    breadth = panel.group_by("date").agg(
        (pl.col("ret_1") > 0).mean().alias("market_breadth"),
        pl.col("ret_1").mean().alias("market_return"),
        pl.col("ret_1").std().alias("market_dispersion"),
    ).sort("date")
    breadth = breadth.with_columns(
        pl.col("market_breadth").rolling_mean(5).alias("market_breadth_5"),
        pl.col("market_return").rolling_mean(5).alias("market_return_mean_5"),
        pl.col("market_return").rolling_mean(20).alias("market_return_mean_20"),
        pl.col("market_return").rolling_std(20).alias("market_volatility_20"),
        pl.col("market_dispersion").rolling_mean(5).alias("market_dispersion_5"),
    )
    panel = panel.join(breadth, on="date", how="left").with_columns(
        (pl.col("ret_1") - pl.col("market_return")).alias("market_relative_return")
    )
    feature_names.extend(
        [
            "market_breadth",
            "market_return",
            "market_dispersion",
            "market_relative_return",
            "market_breadth_5",
            "market_return_mean_5",
            "market_return_mean_20",
            "market_volatility_20",
            "market_dispersion_5",
        ]
    )

    label_expressions = []
    for horizon in HORIZONS:
        entry_open = pl.col("adj_open").shift(-1).over("symbol")
        exit_close = pl.col("adj_close").shift(-horizon).over("symbol")
        future_low = pl.min_horizontal(
            *[pl.col("adj_low").shift(-offset).over("symbol") for offset in range(1, horizon + 1)]
        )
        label_expressions.extend(
            [
                (exit_close / entry_open - 1 - round_trip_fee_bps / 10_000).alias(
                    f"label_return_{horizon}"
                ),
                (future_low / entry_open - 1).alias(f"label_downside_{horizon}"),
            ]
        )
    panel = panel.with_columns(label_expressions)
    eligible_label_row = (~pl.col("is_st")) & (pl.col("listing_age_bars") >= 60)
    panel = panel.with_columns(
        (
            pl.col(f"label_return_{horizon}")
            - pl.when(eligible_label_row)
            .then(pl.col(f"label_return_{horizon}"))
            .otherwise(None)
            .median()
            .over("date")
        ).alias(f"label_excess_return_{horizon}")
        for horizon in HORIZONS
    )
    panel = panel.with_columns(
        pl.col("date").shift(-1).over("symbol").alias("next_trade_date"),
        pl.col("adj_open").shift(-1).over("symbol").alias("next_open"),
        pl.col("adj_close").shift(-1).over("symbol").alias("next_close"),
        pl.col("can_buy_open").shift(-1).over("symbol").alias("next_can_buy"),
        pl.col("can_sell_open").shift(-1).over("symbol").alias("next_can_sell"),
    )

    selected = [
        "date",
        "symbol",
        "ts_code",
        "open",
        "high",
        "low",
        "close",
        "adj_factor",
        "adj_open",
        "adj_high",
        "adj_low",
        "adj_close",
        "can_buy_open",
        "can_sell_open",
        "next_trade_date",
        "next_open",
        "next_close",
        "next_can_buy",
        "next_can_sell",
        "is_st",
        "listing_age_bars",
        "volume",
        "amount",
        *feature_names,
        *[
            name
            for horizon in HORIZONS
            for name in (
                f"label_return_{horizon}",
                f"label_excess_return_{horizon}",
                f"label_downside_{horizon}",
            )
        ],
    ]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    materialized = panel.filter(
        (~pl.col("is_st")) & (pl.col("listing_age_bars") >= 60)
    ).select(selected).collect()
    materialized.write_parquet(output_path, compression="zstd")
    dates = materialized["date"]
    manifest = {
        "feature_version": FEATURE_VERSION,
        "features": feature_names,
        "labels": {
            str(
                horizon
            ): (
                f"D close decision; D+1 open entry; D+{horizon} close exit; "
                f"fee {round_trip_fee_bps}bps; excess vs same-date A-share median"
            )
            for horizon in HORIZONS
        },
        "rows": materialized.height,
        "symbols": materialized["symbol"].n_unique(),
        "start_date": str(dates.min()),
        "end_date": str(dates.max()),
        "as_of_date": as_of_date,
    }
    output_path.with_suffix(".manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", "utf-8"
    )
    return FeatureBuildResult(
        output_path,
        materialized.height,
        materialized["symbol"].n_unique(),
        str(dates.min()),
        str(dates.max()),
        tuple(feature_names),
    )
