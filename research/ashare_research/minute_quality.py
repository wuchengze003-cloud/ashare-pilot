"""Minute-bar quality gates for V1.1 rebound research.

Implements the mandatory quality checks defined in the development plan §M2:
1. OHLC consistency: low <= min(open,close) <= max(open,close) <= high
2. volume >= 0
3. amount >= 0
4. Time monotonic and unique per symbol
5. Time within valid A-share sessions (09:30-11:30, 13:00-15:00)
6. Daily has volume but minute data empty → missing
7. Suspension days don't require minute bars
8. 5min bar count significantly below expected → warning
9. Coverage < 95% → event study fail closed
10. Missing entry/exit bars → no_fill_data_missing (no daily fallback)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import polars as pl

from .minute_data import _valid_5min_times

# Expected 5min bars per full trading day
EXPECTED_5MIN_BARS = 48

# Minimum bar ratio before flagging a warning (e.g. 0.8 = at least 38 bars)
_MIN_BAR_RATIO = 0.75

# Coverage threshold for event study admission
COVERAGE_THRESHOLD_PCT = 95.0

# Increment whenever admission semantics become stricter. Research must not
# reuse a passed artifact produced by an older quality implementation.
QUALITY_REPORT_VERSION = 3


@dataclass
class QualityIssue:
    rule: str
    severity: str  # "error" | "warning"
    count: int
    detail: str = ""


@dataclass
class MinuteQualityReport:
    freq: str
    start_date: str
    end_date: str
    symbols_checked: int
    total_bars: int
    total_rows: int = 0
    issues: list[QualityIssue] = field(default_factory=list)
    coverage_pct: float = 100.0
    passed: bool = True
    failures: list[str] = field(default_factory=list)
    source: str = "tushare_stk_mins"
    generated_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    per_symbol_coverage: dict[str, float] = field(default_factory=dict)
    expected_symbols: list[str] = field(default_factory=list)
    symbol_ranges: dict[str, dict[str, str]] = field(default_factory=dict)
    symbols_with_data: int = 0
    missing_trading_days: int = 0
    missing_bars: int = 0
    zero_turnover_symbol_days: int = 0
    excluded_zero_turnover_bars: int = 0
    unexpected_zero_turnover_symbol_days: int = 0

    def to_dict(self) -> dict[str, Any]:
        issue_counts = {issue.rule: issue.count for issue in self.issues}
        return {
            "quality_version": QUALITY_REPORT_VERSION,
            "source": self.source,
            "generated_at": self.generated_at,
            "freq": self.freq,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "symbols_checked": self.symbols_checked,
            "symbols": self.symbols_checked,
            "symbols_with_data": self.symbols_with_data,
            "total_bars": self.total_bars,
            "total_rows": self.total_rows,
            "zero_turnover_symbol_days": self.zero_turnover_symbol_days,
            "excluded_zero_turnover_bars": self.excluded_zero_turnover_bars,
            "unexpected_zero_turnover_symbol_days": (
                self.unexpected_zero_turnover_symbol_days
            ),
            "duplicate_keys": issue_counts.get("duplicate_time", 0),
            "non_monotonic_time": issue_counts.get("non_monotonic_time", 0),
            "invalid_ohlc": issue_counts.get("ohlc_consistency", 0),
            "invalid_numeric": issue_counts.get("invalid_numeric", 0),
            "missing_required_value": issue_counts.get(
                "missing_required_value", 0
            ),
            "trade_date_mismatch": issue_counts.get("trade_date_mismatch", 0),
            "non_trading_session": issue_counts.get("non_trading_session", 0),
            "missing_trading_days": self.missing_trading_days,
            "missing_bars": self.missing_bars,
            "symbol_ranges": self.symbol_ranges,
            "issues": [
                {"rule": i.rule, "severity": i.severity, "count": i.count, "detail": i.detail}
                for i in self.issues
            ],
            "coverage_pct": self.coverage_pct,
            "per_symbol_coverage": self.per_symbol_coverage,
            "expected_symbols": self.expected_symbols,
            "passed": self.passed,
            "failures": self.failures,
        }


def check_ohlc(df: pl.DataFrame) -> int:
    """Count bars where OHLC relationship is violated."""
    if df.height == 0:
        return 0
    violations = df.filter(
        (pl.col("low") > pl.min_horizontal("open", "close"))
        | (pl.col("high") < pl.max_horizontal("open", "close"))
        | (pl.col("low") > pl.col("high"))
    )
    return violations.height


def check_volume_amount(df: pl.DataFrame) -> tuple[int, int]:
    """Count bars with negative volume or amount."""
    if df.height == 0:
        return 0, 0
    neg_vol = df.filter(pl.col("volume") < 0).height
    neg_amt = df.filter(pl.col("amount") < 0).height
    return neg_vol, neg_amt


def check_required_values(df: pl.DataFrame) -> tuple[int, int, int]:
    """Count invalid numerics, blank identifiers/times, and date mismatches."""
    if df.height == 0:
        return 0, 0, 0
    numeric_columns = ("open", "high", "low", "close", "volume", "amount")
    numeric_exprs = []
    for column in numeric_columns:
        numeric = pl.col(column).cast(pl.Float64, strict=False)
        invalid = numeric.is_null() | ~numeric.is_finite()
        if column in {"open", "high", "low", "close"}:
            invalid = invalid | (numeric <= 0)
        numeric_exprs.append(invalid)
    invalid_numeric = df.filter(pl.any_horizontal(numeric_exprs)).height

    missing_required = df.filter(
        pl.any_horizontal(
            [
                pl.col(column).is_null()
                | (pl.col(column).cast(pl.String).str.len_chars() == 0)
                for column in (
                    "ts_code",
                    "symbol",
                    "trade_date",
                    "trade_time",
                    "freq",
                    "source",
                    "fetched_at",
                )
            ]
        )
    ).height
    normalized_time_date = pl.col("trade_time").cast(pl.String).str.slice(0, 10).str.replace_all("-", "")
    date_mismatch = df.filter(
        normalized_time_date != pl.col("trade_date").cast(pl.String)
    ).height
    return invalid_numeric, missing_required, date_mismatch


def check_time_monotonic(df: pl.DataFrame) -> int:
    """Count duplicate trade_time entries per symbol."""
    if df.height == 0:
        return 0
    dupes = df.height - df.unique(subset=["ts_code", "trade_time"]).height
    return dupes


def check_time_order(df: pl.DataFrame) -> int:
    """Count backwards timestamps in stored row order for each symbol."""
    if df.height == 0:
        return 0
    last_seen: dict[str, str] = {}
    violations = 0
    for ts_code, trade_time in df.select("ts_code", "trade_time").iter_rows():
        symbol = str(ts_code)
        value = str(trade_time)
        previous = last_seen.get(symbol)
        if previous is not None and value < previous:
            violations += 1
        last_seen[symbol] = value
    return violations


def check_trading_session(df: pl.DataFrame, freq: str = "5min") -> int:
    """Count bars outside valid A-share trading sessions."""
    if df.height == 0 or freq != "5min":
        return 0
    valid_times = set(_valid_5min_times())
    non_trading = df.filter(~pl.col("trade_time").str.slice(11, 8).is_in(list(valid_times)))
    return non_trading.height


def check_bar_count(
    df: pl.DataFrame,
    freq: str = "5min",
    expected_bars: int = EXPECTED_5MIN_BARS,
) -> list[dict[str, Any]]:
    """Check per-symbol-per-date bar counts against expected."""
    warnings: list[dict[str, Any]] = []
    if df.height == 0:
        return warnings

    counts = (
        df.group_by(["ts_code", "trade_date"])
        .agg(pl.len().alias("bar_count"))
    )
    low_count = counts.filter(
        pl.col("bar_count") < int(expected_bars * _MIN_BAR_RATIO)
    )
    for row in low_count.iter_rows(named=True):
        warnings.append({
            "ts_code": row["ts_code"],
            "trade_date": row["trade_date"],
            "bar_count": row["bar_count"],
            "expected": expected_bars,
        })
    return warnings


def check_daily_minute_gap(
    minute_df: pl.DataFrame,
    daily_dates_with_volume: set[str],
    suspended_dates: set[str],
    ts_code: str,
) -> list[str]:
    """Find dates where daily has volume but minute data is empty.

    Args:
        minute_df: Minute bars for this symbol.
        daily_dates_with_volume: Set of YYYYMMDD dates with daily volume > 0.
        suspended_dates: Set of YYYYMMDD suspension dates.
        ts_code: Symbol ts_code.

    Returns:
        List of missing date strings.
    """
    if minute_df.height > 0:
        minute_dates = set(minute_df["trade_date"].unique().to_list())
    else:
        minute_dates = set()

    missing = []
    for d in sorted(daily_dates_with_volume):
        if d in suspended_dates:
            continue
        if d not in minute_dates:
            missing.append(d)
    return missing


def _find_zero_turnover_days(df: pl.DataFrame) -> pl.DataFrame:
    """Return symbol-days whose supplied bars all have zero turnover."""
    if df.is_empty():
        return pl.DataFrame(
            schema={"ts_code": pl.String, "trade_date": pl.String}
        )
    return (
        df.group_by("ts_code", "trade_date")
        .agg(
            (
                pl.col("volume")
                .cast(pl.Float64, strict=False)
                .fill_null(float("nan"))
                .eq(0)
                .all()
            ).alias("_all_zero_volume"),
            (
                pl.col("amount")
                .cast(pl.Float64, strict=False)
                .fill_null(float("nan"))
                .eq(0)
                .all()
            ).alias("_all_zero_amount"),
            pl.len().alias("_bar_count"),
        )
        .filter(
            pl.col("_all_zero_volume") & pl.col("_all_zero_amount")
        )
        .select("ts_code", "trade_date", "_bar_count")
    )


def run_minute_quality(
    minute_root: Path | str,
    start_date: str,
    end_date: str,
    freq: str = "5min",
    trading_dates: list[str] | None = None,
    daily_volume_map: dict[str, set[str]] | None = None,
    suspended_map: dict[str, set[str]] | None = None,
    expected_symbols: list[str] | None = None,
) -> MinuteQualityReport:
    """Run all quality checks on the minute data warehouse.

    Args:
        minute_root: Path to minute data root.
        start_date: YYYY-MM-DD start.
        end_date: YYYY-MM-DD end.
        freq: Bar frequency.
        trading_dates: Expected trading dates (YYYYMMDD) for coverage.
        daily_volume_map: {ts_code: set of YYYYMMDD with volume > 0}.
        suspended_map: {ts_code: set of YYYYMMDD suspended dates}.
        expected_symbols: Full list of ts_codes that MUST have minute data.
            Symbols in this list with zero minute bars will fail coverage.
    """
    minute_root = Path(minute_root)
    freq_root = minute_root / "raw" / f"freq={freq}"

    report = MinuteQualityReport(
        freq=freq,
        start_date=start_date,
        end_date=end_date,
        symbols_checked=0,
        total_bars=0,
        expected_symbols=sorted(set(expected_symbols or [])),
    )
    expected_symbol_set = set(report.expected_symbols)

    if not freq_root.exists():
        report.passed = False
        report.failures.append("no minute data directory found")
        return report

    start_compact = start_date.replace("-", "")
    end_compact = end_date.replace("-", "")

    all_frames: list[pl.DataFrame] = []
    symbols_found: list[str] = []

    for ts_dir in sorted(freq_root.iterdir()):
        if not ts_dir.is_dir() or not ts_dir.name.startswith("ts_code="):
            continue
        ts_code = ts_dir.name.removeprefix("ts_code=")
        if expected_symbol_set and ts_code not in expected_symbol_set:
            continue
        raw_frames: list[pl.DataFrame] = []
        for parquet_file in sorted(ts_dir.rglob("part.parquet")):
            try:
                frame = pl.read_parquet(parquet_file)
            except Exception as error:
                report.failures.append(
                    f"unreadable minute partition {parquet_file}: {error}"
                )
                continue
            if "trade_date" not in frame.columns:
                report.failures.append(
                    f"minute partition missing trade_date: {parquet_file}"
                )
                continue
            frame = (
                frame.with_columns(
                    pl.col("trade_date").cast(pl.String),
                    pl.lit(ts_code).alias("_partition_ts_code"),
                )
                .filter(
                    (pl.col("trade_date") >= start_compact)
                    & (pl.col("trade_date") <= end_compact)
                )
            )
            if frame.height > 0:
                raw_frames.append(frame)
        df = (
            pl.concat(raw_frames, how="diagonal_relaxed")
            if raw_frames
            else pl.DataFrame()
        )
        if df.height > 0:
            all_frames.append(df)
            symbols_found.append(ts_code)

    all_expected = set(expected_symbol_set or symbols_found)
    if daily_volume_map:
        daily_symbols = set(daily_volume_map)
        all_expected |= (
            daily_symbols & expected_symbol_set
            if expected_symbol_set
            else daily_symbols
        )
    report.symbols_checked = len(all_expected)

    if not all_frames:
        report.passed = False
        report.failures.append("no minute data found in range")
        report.coverage_pct = 0.0
        report.per_symbol_coverage = {
            symbol: 0.0 for symbol in sorted(all_expected)
        }
        return report

    full = pl.concat(all_frames, how="diagonal_relaxed")
    report.total_rows = full.height
    required_columns = {
        "ts_code",
        "symbol",
        "trade_date",
        "trade_time",
        "freq",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "amount",
        "source",
        "realtime",
        "fetched_at",
    }
    missing_columns = sorted(required_columns - set(full.columns))
    if missing_columns:
        report.passed = False
        report.failures.append(
            f"minute data missing required columns: {missing_columns}"
        )
        return report
    invalid_sources = full.filter(pl.col("source") != "tushare_stk_mins").height
    if invalid_sources:
        report.issues.append(
            QualityIssue("invalid_source", "error", invalid_sources)
        )
    partition_symbol_mismatches = full.filter(
        pl.col("ts_code").cast(pl.String)
        != pl.col("_partition_ts_code").cast(pl.String)
    ).height
    if partition_symbol_mismatches:
        report.issues.append(
            QualityIssue(
                "partition_symbol_mismatch",
                "error",
                partition_symbol_mismatches,
            )
        )
    symbol_mismatches = full.filter(
        pl.col("symbol").cast(pl.String)
        != pl.col("ts_code").cast(pl.String).str.split(".").list.first()
    ).height
    if symbol_mismatches:
        report.issues.append(
            QualityIssue("symbol_mismatch", "error", symbol_mismatches)
        )
    invalid_frequency = full.filter(
        pl.col("freq").cast(pl.String) != freq
    ).height
    if invalid_frequency:
        report.issues.append(
            QualityIssue("invalid_frequency", "error", invalid_frequency)
        )
    realtime = pl.col("realtime").cast(pl.Boolean, strict=False)
    invalid_realtime = full.filter(realtime.is_null() | realtime).height
    if invalid_realtime:
        report.issues.append(
            QualityIssue("invalid_realtime", "error", invalid_realtime)
        )
    invalid_numeric, missing_required, date_mismatch = check_required_values(full)
    if invalid_numeric:
        report.issues.append(
            QualityIssue("invalid_numeric", "error", invalid_numeric)
        )
    if missing_required:
        report.issues.append(
            QualityIssue("missing_required_value", "error", missing_required)
        )
    if date_mismatch:
        report.issues.append(
            QualityIssue("trade_date_mismatch", "error", date_mismatch)
        )
    # Check 1: OHLC
    ohlc_violations = check_ohlc(full)
    if ohlc_violations > 0:
        report.issues.append(QualityIssue("ohlc_consistency", "error", ohlc_violations))

    # Check 2-3: Volume and amount
    neg_vol, neg_amt = check_volume_amount(full)
    if neg_vol > 0:
        report.issues.append(QualityIssue("negative_volume", "error", neg_vol))
    if neg_amt > 0:
        report.issues.append(QualityIssue("negative_amount", "error", neg_amt))

    # Supplier zero-turnover shells remain in raw storage for audit, but they
    # are not valid market bars. A suspension record or absence from the
    # positive-volume daily map explains a non-trading day. If daily says the
    # stock traded, the all-zero minute day is a hard data error.
    zero_days = _find_zero_turnover_days(full)
    explainable_keys: list[tuple[str, str]] = []
    unexpected_keys: list[tuple[str, str]] = []
    if not zero_days.is_empty():
        for row in zero_days.iter_rows(named=True):
            ts_code = str(row["ts_code"])
            trade_date = str(row["trade_date"])
            suspended = trade_date in (suspended_map or {}).get(
                ts_code, set()
            )
            absent_from_positive_daily = (
                daily_volume_map is not None
                and trade_date not in daily_volume_map.get(ts_code, set())
            )
            target = (
                explainable_keys
                if suspended or absent_from_positive_daily
                else unexpected_keys
            )
            target.append((ts_code, trade_date))

        zero_keys = zero_days.select("ts_code", "trade_date")
        tradable = full.join(
            zero_keys,
            on=["ts_code", "trade_date"],
            how="anti",
        )
        report.zero_turnover_symbol_days = zero_days.height
        report.excluded_zero_turnover_bars = int(
            zero_days["_bar_count"].sum()
        )
        report.unexpected_zero_turnover_symbol_days = len(unexpected_keys)
        if explainable_keys:
            report.issues.append(
                QualityIssue(
                    "zero_turnover_non_trading_day",
                    "warning",
                    len(explainable_keys),
                    (
                        "excluded from effective bars; first: "
                        f"{explainable_keys[0]}"
                    ),
                )
            )
        if unexpected_keys:
            report.issues.append(
                QualityIssue(
                    "unexpected_zero_turnover_day",
                    "error",
                    len(unexpected_keys),
                    (
                        "daily has positive volume but minute bars have no "
                        f"turnover; first: {unexpected_keys[0]}"
                    ),
                )
            )
    else:
        tradable = full

    report.total_bars = tradable.height
    tradable_symbols = (
        tradable["ts_code"].cast(pl.String).unique().sort().to_list()
        if tradable.height > 0
        else []
    )
    report.symbols_with_data = len(tradable_symbols)
    for ts_code in tradable_symbols:
        symbol_frame = tradable.filter(pl.col("ts_code") == ts_code)
        report.symbol_ranges[ts_code] = {
            "first_time": str(symbol_frame["trade_time"].min()),
            "last_time": str(symbol_frame["trade_time"].max()),
        }

    # Check 4: Time monotonic/unique
    dupes = check_time_monotonic(full)
    if dupes > 0:
        report.issues.append(QualityIssue("duplicate_time", "error", dupes))
    time_order_violations = check_time_order(full)
    if time_order_violations > 0:
        report.issues.append(
            QualityIssue("non_monotonic_time", "error", time_order_violations)
        )

    # Check 5: Trading session
    non_trading = check_trading_session(full, freq)
    if non_trading > 0:
        report.issues.append(QualityIssue("non_trading_session", "error", non_trading))

    # Check 8: Bar count warnings
    bar_warnings = check_bar_count(tradable, freq)
    if bar_warnings:
        expected_bars = EXPECTED_5MIN_BARS if freq == "5min" else 0
        report.missing_bars += sum(
            max(0, expected_bars - int(item["bar_count"]))
            for item in bar_warnings
        )
        report.issues.append(
            QualityIssue("low_bar_count", "warning", len(bar_warnings),
                         f"first: {bar_warnings[0]}")
        )

    # Check 6: Daily-minute gap — check ALL expected symbols, not just found.
    # CRITICAL: A symbol with daily volume but ZERO minute data must be caught.
    if daily_volume_map:
        total_missing = 0
        for ts_code in sorted(all_expected):
            daily_dates = daily_volume_map.get(ts_code, set())
            suspended = (suspended_map or {}).get(ts_code, set())
            sym_df = tradable.filter(pl.col("ts_code") == ts_code)
            missing = check_daily_minute_gap(sym_df, daily_dates, suspended, ts_code)
            total_missing += len(missing)
        if total_missing > 0:
            report.missing_trading_days = total_missing
            if freq == "5min":
                report.missing_bars += total_missing * EXPECTED_5MIN_BARS
            report.issues.append(
                QualityIssue("daily_minute_gap", "error", total_missing)
            )

    # Check 9: Coverage — PER-SYMBOL, not union across pool.
    # CRITICAL: If trading_dates not provided, we CANNOT verify coverage.
    # Fail closed to prevent false passes.
    if trading_dates:
        expected_dates = set(trading_dates)
        total_expected = len(expected_dates)
        if total_expected == 0:
            report.coverage_pct = 0.0
            report.failures.append("trading_dates is empty: cannot verify coverage")
        else:
            # Per-symbol coverage: EVERY expected symbol must independently
            # meet the threshold. A symbol completely missing minute data = 0%.
            failing_symbols: list[str] = []

            for sym in sorted(all_expected):
                # Get dates this symbol should have data
                if daily_volume_map is not None:
                    sym_expected = (
                        daily_volume_map.get(sym, set()) & expected_dates
                    )
                else:
                    sym_expected = expected_dates
                # Subtract suspended dates
                if suspended_map and sym in suspended_map:
                    sym_expected = sym_expected - suspended_map[sym]

                if not sym_expected:
                    report.per_symbol_coverage[sym] = 100.0
                    continue

                # Get actual minute dates for this symbol
                sym_df = tradable.filter(pl.col("ts_code") == sym)
                if sym_df.height > 0:
                    sym_actual = set(sym_df["trade_date"].unique().to_list())
                else:
                    sym_actual = set()

                present = len(sym_actual & sym_expected)
                sym_cov = round((present / len(sym_expected)) * 100.0, 2)
                report.per_symbol_coverage[sym] = sym_cov
                if sym_cov < COVERAGE_THRESHOLD_PCT:
                    failing_symbols.append(f"{sym}={sym_cov}%")

            # Overall coverage is the minimum across symbols (fail-closed)
            if report.per_symbol_coverage:
                report.coverage_pct = min(report.per_symbol_coverage.values())
            else:
                report.coverage_pct = 0.0

            if failing_symbols:
                report.failures.append(
                    f"per-symbol coverage below {COVERAGE_THRESHOLD_PCT}%: "
                    + "; ".join(failing_symbols[:10])
                )
    else:
        # No trading calendar provided - cannot verify coverage, fail closed
        report.coverage_pct = 0.0
        report.failures.append(
            "trading_dates not provided: cannot verify coverage, fail closed"
        )

    # Determine pass/fail
    error_issues = [i for i in report.issues if i.severity == "error"]
    if error_issues:
        report.failures.extend(
            f"{i.rule}: {i.count} violations" for i in error_issues
        )
    report.passed = len(report.failures) == 0

    return report
