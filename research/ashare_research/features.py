"""Polars feature panel with execution-aligned multi-horizon labels."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

import polars as pl

from .contracts import HORIZONS
from .cost_config import load_cost_model

FEATURE_VERSION = "ashare-core-v5"
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
    moneyflow_available: bool = True


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


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _membership_intervals(path: Path) -> pl.DataFrame:
    membership = pl.read_parquet(path)
    required = {"instrument", "member_start", "member_end"}
    missing = required - set(membership.columns)
    if missing:
        raise ValueError(
            f"universe membership is missing columns: {sorted(missing)}"
        )
    return (
        membership.select(sorted(required))
        .with_columns(
            pl.col("instrument").cast(pl.String).str.to_lowercase().alias("symbol"),
            pl.col("member_start").cast(pl.Date),
            pl.col("member_end").cast(pl.Date),
        )
        .drop("instrument")
        .sort("symbol", "member_start")
    )


def _ever_member_symbols(membership: pl.DataFrame) -> tuple[str, ...]:
    symbols = tuple(
        sorted(
            {
                str(value).lower()
                for value in membership["symbol"].drop_nulls().to_list()
            }
        )
    )
    if len(symbols) < 800:
        raise ValueError(
            f"universe membership has only {len(symbols)} distinct symbols"
        )
    return symbols


def _attach_point_in_time_membership(
    panel: pl.LazyFrame,
    membership: pl.DataFrame,
) -> pl.LazyFrame:
    return (
        panel.sort("symbol", "date")
        .join_asof(
            membership.lazy(),
            left_on="date",
            right_on="member_start",
            by="symbol",
            strategy="backward",
            check_sortedness=False,
        )
        .with_columns(
            (
                pl.col("member_start").is_not_null()
                & (pl.col("date") <= pl.col("member_end"))
            ).alias("__is_universe_member")
        )
        .drop("member_start", "member_end")
    )


def _point_in_time_market_aggregates(panel: pl.LazyFrame) -> pl.LazyFrame:
    return (
        panel.filter(pl.col("__is_universe_member"))
        .group_by("date")
        .agg(
            (pl.col("ret_1") > 0).mean().alias("market_breadth"),
            pl.col("ret_1").mean().alias("market_return"),
            pl.col("ret_1").std().alias("market_dispersion"),
        )
        .sort("date")
        .with_columns(
            pl.col("market_breadth").rolling_mean(5).alias("market_breadth_5"),
            pl.col("market_return").rolling_mean(5).alias("market_return_mean_5"),
            pl.col("market_return").rolling_mean(20).alias("market_return_mean_20"),
            pl.col("market_return").rolling_std(20).alias("market_volatility_20"),
            pl.col("market_dispersion").rolling_mean(5).alias("market_dispersion_5"),
        )
    )


def build_feature_panel(
    data_root: Path | str,
    output_path: Path | str,
    round_trip_fee_bps: float | None = None,
    as_of_date: str | None = None,
    universe_membership_path: Path | str | None = None,
) -> FeatureBuildResult:
    if round_trip_fee_bps is None:
        round_trip_fee_bps = load_cost_model().round_trip_bps
    if round_trip_fee_bps < 0:
        raise ValueError("round_trip_fee_bps must be non-negative")
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
    moneyflow_available = False
    if moneyflow is not None:
        panel = panel.join(moneyflow, on=["ts_code", "trade_date"], how="left")
        # Check if moneyflow data is actually present and non-trivial.
        # A broken feed can produce all-null or all-zero columns, which
        # would silently degenerate the moneyflow features to fake zeros.
        mf_stats = panel.select(
            pl.len().alias("rows"),
            pl.col("net_mf_amount").is_not_null().sum().alias("non_null"),
            (pl.col("net_mf_amount").cast(pl.Float64, strict=False) != 0.0)
            .fill_null(False)
            .sum()
            .alias("non_zero"),
        ).collect()
        total_rows = max(int(mf_stats["rows"][0]), 1)
        non_null = int(mf_stats["non_null"][0])
        non_zero = int(mf_stats["non_zero"][0])
        coverage = non_null / total_rows
        non_zero_rate = non_zero / max(non_null, 1)
        if coverage > 0.5 and non_zero_rate > 0.01:
            moneyflow_available = True
        else:
            # Data source exists but is degenerate — keep columns as null
            # and mark as unavailable so downstream can skip the features.
            pass
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
            "net_mf_amount": None,
            "buy_lg_amount": None,
            "sell_lg_amount": None,
            "is_suspended": False,
        },
    )
    panel = panel.with_columns(
        pl.col("trade_date").cast(pl.String).str.strptime(pl.Date, "%Y%m%d").alias("date"),
        pl.col("ts_code").map_elements(canonical_symbol, return_dtype=pl.String).alias("symbol"),
        pl.col("adj_factor").cast(pl.Float64, strict=False),
        pl.col("is_suspended").fill_null(False),
        # Tushare daily.amount is 千元 while moneyflow amounts are 万元.
        # Normalize both to人民币元 before capacity and ratio calculations.
        (pl.col("amount").cast(pl.Float64) * 1_000.0).alias("amount"),
        (pl.col("net_mf_amount").cast(pl.Float64) * 10_000.0).alias(
            "net_mf_amount"
        ),
        (pl.col("buy_lg_amount").cast(pl.Float64) * 10_000.0).alias(
            "buy_lg_amount"
        ),
        (pl.col("sell_lg_amount").cast(pl.Float64) * 10_000.0).alias(
            "sell_lg_amount"
        ),
    ).sort("symbol", "date")
    membership_path = (
        Path(universe_membership_path)
        if universe_membership_path is not None
        else data_root / "reference" / "csi800_membership.parquet"
    )
    materialized_universe = "all-a-share"
    membership_sha256: str | None = None
    membership_intervals: pl.DataFrame | None = None
    if membership_path.is_file():
        membership_intervals = _membership_intervals(membership_path)
        panel = panel.filter(
            pl.col("symbol").is_in(_ever_member_symbols(membership_intervals))
        )
        panel = _attach_point_in_time_membership(panel, membership_intervals)
        materialized_universe = "point-in-time-csi800"
        membership_sha256 = _file_sha256(membership_path)
    else:
        panel = panel.with_columns(
            pl.lit(True).alias("__is_universe_member")
        )
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
    breadth = _point_in_time_market_aggregates(panel)

    feature_names = ["kmid", "klen", "kup", "klow", "ret_1"]
    expressions: list[pl.Expr] = []
    for window in PRICE_WINDOWS:
        name = f"momentum_{window}"
        expressions.append(
            (pl.col("adj_close") / pl.col("adj_close").shift(window).over("symbol") - 1).alias(name)
        )
        feature_names.append(name)
    panel = panel.with_columns(
        pl.col("adj_close").rolling_min(60).over("symbol").alias("__low_60"),
        pl.col("adj_close").rolling_max(60).over("symbol").alias("__high_60"),
    ).with_columns(
        (
            (pl.col("adj_close") - pl.col("__low_60"))
            / (pl.col("__high_60") - pl.col("__low_60"))
        )
        .fill_nan(None)
        .alias("position_60"),
        (pl.col("adj_close") / pl.col("__high_60") - 1).alias("drawdown_60"),
    )
    feature_names.extend(["position_60", "drawdown_60"])
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
    # Non-moneyflow features (always computed)
    expressions.extend(
        [
            (pl.col("true_range").rolling_mean(14).over("symbol") / pl.col("adj_close")).alias(
                "atr_14_pct"
            ),
            (
                pl.col("turnover_rate") / pl.col("turnover_rate").rolling_mean(20).over("symbol")
            ).alias("turnover_ratio_20"),
            pl.when(pl.col("pe_ttm").is_not_null() & (pl.col("pe_ttm") > 0))
            .then(pl.col("pe_ttm").log1p())
            .otherwise(None)
            .cast(pl.Float64)
            .alias("log_pe_ttm"),
            pl.col("pb").log1p().alias("log_pb"),
            pl.col("total_mv").log1p().alias("log_market_cap"),
        ]
    )
    # Moneyflow features: only compute when data is available.
    # When moneyflow is broken/unavailable, keep null so the model and
    # quality checks can detect the absence via missing-rate rather than
    # silently training on fake zeros.
    if moneyflow_available:
        expressions.extend(
            [
                (pl.col("net_mf_amount") / (pl.col("amount").abs() + 1)).alias(
                    "net_moneyflow_ratio"
                ),
                (
                    (pl.col("buy_lg_amount") - pl.col("sell_lg_amount"))
                    / (pl.col("amount").abs() + 1)
                ).alias("large_order_ratio"),
            ]
        )
    else:
        expressions.extend(
            [
                pl.lit(None).cast(pl.Float64).alias("net_moneyflow_ratio"),
                pl.lit(None).cast(pl.Float64).alias("large_order_ratio"),
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

    maximum_horizon = max(HORIZONS)
    calendar = panel.select("date").unique().sort("date").with_columns(
        pl.col("date").shift(-offset).alias(f"__date_plus_{offset}")
        for offset in range(1, maximum_horizon + 1)
    )
    market_rows = panel.select(
        "symbol",
        "date",
        "adj_open",
        "adj_low",
        "adj_close",
        "open",
        "close",
        "adj_factor",
        "up_limit",
        "down_limit",
        "is_suspended",
        "can_buy_open",
        "can_sell_open",
    )
    panel = panel.join(calendar, on="date", how="left")
    for offset in range(1, maximum_horizon + 1):
        future = market_rows.select(
            "symbol",
            pl.col("date").alias("__target_date"),
            pl.col("adj_open").alias(f"__adj_open_{offset}"),
            pl.col("adj_low").alias(f"__adj_low_{offset}"),
            pl.col("adj_close").alias(f"__adj_close_{offset}"),
            pl.col("open").alias(f"__raw_open_{offset}"),
            pl.col("close").alias(f"__raw_close_{offset}"),
            pl.col("adj_factor").alias(f"__adj_factor_{offset}"),
            pl.col("up_limit").alias(f"__up_limit_{offset}"),
            pl.col("down_limit").alias(f"__down_limit_{offset}"),
            pl.col("is_suspended").alias(f"__is_suspended_{offset}"),
            pl.col("can_buy_open").alias(f"__can_buy_{offset}"),
            pl.col("can_sell_open").alias(f"__can_sell_{offset}"),
        )
        panel = panel.join(
            future,
            left_on=["symbol", f"__date_plus_{offset}"],
            right_on=["symbol", "__target_date"],
            how="left",
        )

    label_expressions = []
    for horizon in HORIZONS:
        entry_open = pl.col("__adj_open_1")
        exit_close = pl.col(f"__adj_close_{horizon}")
        required_future_prices = pl.all_horizontal(
            [
                pl.col("__adj_open_1").is_not_null(),
                pl.col(f"__adj_close_{horizon}").is_not_null(),
                *[
                    pl.col(f"__adj_low_{offset}").is_not_null()
                    for offset in range(1, horizon + 1)
                ],
            ]
        )
        future_low = pl.min_horizontal(
            *[
                pl.col(f"__adj_low_{offset}")
                for offset in range(1, horizon + 1)
            ]
        )
        label_expressions.extend(
            [
                pl.when(required_future_prices)
                .then(
                    exit_close
                    / entry_open
                    - 1
                    - round_trip_fee_bps
                    / 10_000
                )
                .otherwise(None)
                .alias(f"label_return_{horizon}"),
                pl.when(required_future_prices)
                .then(future_low / entry_open - 1)
                .otherwise(None)
                .alias(f"label_downside_{horizon}"),
            ]
        )
    panel = panel.with_columns(label_expressions)
    eligible_label_row = (
        pl.col("__is_universe_member")
        & (~pl.col("is_st"))
        & (pl.col("listing_age_bars") >= 60)
    )
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
        pl.col("__date_plus_1").alias("next_trade_date"),
        pl.col("__raw_open_1").alias("next_raw_open"),
        pl.col("__raw_close_1").alias("next_raw_close"),
        pl.col("__adj_factor_1").alias("next_adj_factor"),
        pl.col("__up_limit_1").alias("next_up_limit"),
        pl.col("__down_limit_1").alias("next_down_limit"),
        pl.col("__is_suspended_1").alias("next_is_suspended"),
        pl.col("__adj_open_1").alias("next_open"),
        pl.col("__adj_close_1").alias("next_close"),
        pl.col("__can_buy_1").alias("next_can_buy"),
        pl.col("__can_sell_1").alias("next_can_sell"),
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
        "next_raw_open",
        "next_raw_close",
        "next_adj_factor",
        "next_up_limit",
        "next_down_limit",
        "next_is_suspended",
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
    eligible_panel = panel.filter(
        pl.col("__is_universe_member")
        & (~pl.col("is_st"))
        & (pl.col("listing_age_bars") >= 60)
    )
    materialized = eligible_panel.select(selected).collect()
    missing_adjustment = int(materialized["adj_factor"].null_count())
    if missing_adjustment:
        raise ValueError(
            f"adj_factor missing for {missing_adjustment} feature-panel rows"
        )
    missing_execution = materialized.filter(
        pl.col("next_trade_date").is_not_null()
        & pl.col("next_raw_open").is_not_null()
        & (
            pl.col("next_can_buy").is_null()
            | pl.col("next_can_sell").is_null()
            | pl.col("next_is_suspended").is_null()
            | (
                (~pl.col("next_is_suspended"))
                & (
                    pl.col("next_up_limit").is_null()
                    | pl.col("next_down_limit").is_null()
                    | pl.col("next_adj_factor").is_null()
                )
            )
        )
    ).height
    if missing_execution:
        raise ValueError(
            "price-limit execution flags missing for "
            f"{missing_execution} feature-panel rows"
        )
    fd, temporary = tempfile.mkstemp(
        prefix=output_path.name,
        suffix=".tmp",
        dir=output_path.parent,
    )
    os.close(fd)
    try:
        materialized.write_parquet(temporary, compression="zstd")
        os.replace(temporary, output_path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
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
        "as_of_date": str(dates.max()),
        "moneyflow_available": moneyflow_available,
        "materialized_universe": materialized_universe,
        "market_aggregate_universe": materialized_universe,
        "universe_membership_sha256": membership_sha256,
        "units": {
            "amount": "CNY",
            "net_mf_amount": "CNY",
            "buy_lg_amount": "CNY",
            "sell_lg_amount": "CNY",
            "volume": "lots",
        },
    }
    manifest_path = output_path.with_suffix(".manifest.json")
    manifest_temporary = manifest_path.with_suffix(
        manifest_path.suffix + ".tmp"
    )
    manifest_temporary.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        "utf-8",
    )
    os.replace(manifest_temporary, manifest_path)
    return FeatureBuildResult(
        output_path,
        materialized.height,
        materialized["symbol"].n_unique(),
        str(dates.min()),
        str(dates.max()),
        tuple(feature_names),
        moneyflow_available,
    )
