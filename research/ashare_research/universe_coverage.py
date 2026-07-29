"""Historical universe coverage audit (merge blocker 2).

Verifies that every stock that ever entered the CSI800 point-in-time
membership since 2018 has usable daily market data, and that delisted
members are not silently dropped. Produces a machine-readable coverage
report; severe gaps fail closed (non-zero exit via ``main`` return code).

Definitions:
- expected_member_days: open-calendar days inside a membership interval.
- actual_member_days: daily bars present inside that interval.
- suspended_member_days: suspend_d rows inside that interval (no bar is
  expected on those days, so they are not "missing").
- missing_member_days = expected - actual - suspended (clamped at 0).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, UTC
from pathlib import Path
from typing import Any

import polars as pl

# A member whose adjusted coverage is below this ratio counts as a gap.
COVERAGE_GAP_THRESHOLD = 0.95
# The audit fails when more than this many members are gap members.
MAX_GAP_MEMBERS = 0
# The audit fails when any member listed as still trading (list_status=L)
# has zero daily bars inside its membership intervals — that is the classic
# silent-skip failure mode.
MAX_SILENTLY_SKIPPED = 0


def _canonical_symbol(ts_code: str) -> str:
    code, exchange = ts_code.split(".", 1)
    prefix = {"SH": "sh", "SZ": "sz", "BJ": "bj"}.get(exchange.upper())
    if not prefix:
        raise ValueError(f"unsupported exchange: {ts_code}")
    return f"{prefix}{code}"


def _load_membership(root: Path) -> pl.DataFrame:
    path = root / "reference" / "csi800_membership.parquet"
    if not path.is_file():
        raise FileNotFoundError(f"membership parquet missing: {path}")
    frame = pl.read_parquet(path)
    required = {"instrument", "member_start", "member_end"}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"membership parquet missing columns: {sorted(missing)}")
    return frame.with_columns(
        pl.col("instrument").cast(pl.String).str.to_lowercase().alias("symbol"),
        pl.col("member_start").cast(pl.Date),
        pl.col("member_end").cast(pl.Date),
    ).select("symbol", "member_start", "member_end")


def _load_stock_basic(root: Path) -> pl.DataFrame:
    path = root / "reference" / "stock_basic.parquet"
    if not path.is_file():
        raise FileNotFoundError(f"stock_basic parquet missing: {path}")
    frame = pl.read_parquet(path)
    if "ts_code" not in frame.columns or "list_status" not in frame.columns:
        raise ValueError("stock_basic parquet missing ts_code/list_status")
    return frame.with_columns(
        pl.col("ts_code")
        .cast(pl.String)
        .map_elements(_canonical_symbol, return_dtype=pl.String)
        .alias("symbol")
    )


def _attach_membership(
    frame: pl.LazyFrame, membership: pl.DataFrame
) -> pl.LazyFrame:
    """Mark rows that fall inside a membership interval (per symbol)."""
    return (
        frame.sort("symbol", "date")
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
            ).alias("__is_member")
        )
        .drop("member_start", "member_end")
    )


def build_historical_universe_coverage(
    data_root: Path | str,
    since: str = "2018-01-01",
    expected_member_count: int = 800,
) -> dict[str, Any]:
    root = Path(data_root)
    since_date = datetime.strptime(since, "%Y-%m-%d").date()

    membership = _load_membership(root).filter(
        pl.col("member_end") >= since_date
    )
    if membership.is_empty():
        raise ValueError("no membership intervals overlap the audit window")
    member_symbols = sorted(membership["symbol"].unique().to_list())

    stock_basic = _load_stock_basic(root)
    status_counts = {
        str(status): int(count)
        for status, count in stock_basic.group_by("list_status")
        .len()
        .iter_rows()
    }

    calendar_path = root / "reference" / "trade_cal.parquet"
    calendar = pl.read_parquet(calendar_path)
    if "is_open" in calendar.columns:
        calendar = calendar.filter(pl.col("is_open").cast(pl.Int64, strict=False) == 1)
    calendar_days = sorted(
        str(value) for value in calendar["cal_date"].cast(pl.String).to_list()
    )

    # --- expected member-days: open-calendar days inside membership intervals
    # clipped to the stock's listed life [list_date, delist_date], so a member
    # delisted before the next index rebalance is not "missing" bars.
    stock_life = stock_basic.select(
        "symbol",
        pl.col("list_date").cast(pl.String).alias("list_date"),
        pl.col("delist_date").cast(pl.String).alias("delist_date"),
    ).with_columns(
        pl.col("list_date").str.strptime(pl.Date, "%Y%m%d", strict=False),
        pl.col("delist_date").str.strptime(pl.Date, "%Y%m%d", strict=False),
    )
    membership_clipped = (
        membership.join(stock_life, on="symbol", how="left")
        .with_columns(
            pl.max_horizontal("member_start", "list_date").alias("member_start"),
            pl.when(pl.col("delist_date").is_not_null())
            .then(pl.min_horizontal("member_end", "delist_date"))
            .otherwise(pl.col("member_end"))
            .alias("member_end"),
        )
        .filter(pl.col("member_start") <= pl.col("member_end"))
        .select("symbol", "member_start", "member_end")
    )
    cal = pl.DataFrame({"cal_date": calendar_days}).with_columns(
        pl.col("cal_date").str.strptime(pl.Date, "%Y%m%d").alias("date")
    )
    cal_members = (
        cal.lazy()
        .join(membership_clipped.lazy(), how="cross")
        .filter(
            (pl.col("date") >= pl.col("member_start"))
            & (pl.col("date") <= pl.col("member_end"))
        )
        .group_by("symbol")
        .len()
        .collect()
        .rename({"len": "expected_days"})
    )

    # --- actual member-days: daily bars inside membership intervals
    daily = (
        pl.scan_parquet(str(root / "raw" / "daily" / "**" / "*.parquet"))
        .select("ts_code", "trade_date")
        .with_columns(
            pl.col("trade_date").cast(pl.String).str.strptime(pl.Date, "%Y%m%d").alias("date"),
            pl.col("ts_code")
            .cast(pl.String)
            .map_elements(_canonical_symbol, return_dtype=pl.String)
            .alias("symbol"),
        )
        .select("symbol", "date")
        .filter(pl.col("symbol").is_in(member_symbols))
    )
    daily_members = (
        _attach_membership(daily, membership_clipped)
        .filter(pl.col("__is_member"))
        .group_by("symbol")
        .len()
        .collect()
        .rename({"len": "actual_days"})
    )

    # --- suspended member-days
    suspended_path = root / "raw" / "suspend_d"
    if suspended_path.exists() and any(suspended_path.rglob("*.parquet")):
        suspended = (
            pl.scan_parquet(str(suspended_path / "**" / "*.parquet"))
            .select("ts_code", "trade_date")
            .with_columns(
                pl.col("trade_date")
                .cast(pl.String)
                .str.strptime(pl.Date, "%Y%m%d")
                .alias("date"),
                pl.col("ts_code")
                .cast(pl.String)
                .map_elements(_canonical_symbol, return_dtype=pl.String)
                .alias("symbol"),
            )
            .select("symbol", "date")
            .unique()
            .filter(pl.col("symbol").is_in(member_symbols))
        )
        suspended_members = (
            _attach_membership(suspended, membership_clipped)
            .filter(pl.col("__is_member"))
            .group_by("symbol")
            .len()
            .collect()
            .rename({"len": "suspended_days"})
        )
    else:
        suspended_members = pl.DataFrame(
            {"symbol": member_symbols, "suspended_days": [0] * len(member_symbols)}
        )

    coverage = (
        pl.DataFrame({"symbol": member_symbols})
        .join(cal_members, on="symbol", how="left")
        .join(daily_members, on="symbol", how="left")
        .join(suspended_members, on="symbol", how="left")
        .with_columns(
            pl.col("expected_days").fill_null(0).cast(pl.Int64),
            pl.col("actual_days").fill_null(0).cast(pl.Int64),
            pl.col("suspended_days").fill_null(0).cast(pl.Int64),
        )
        .with_columns(
            (
                pl.col("expected_days")
                - pl.col("actual_days")
                - pl.col("suspended_days")
            )
            .clip(lower_bound=0)
            .alias("missing_days")
        )
        .with_columns(
            pl.when((pl.col("expected_days") - pl.col("suspended_days")) > 0)
            .then(
                pl.col("actual_days")
                / (pl.col("expected_days") - pl.col("suspended_days"))
            )
            .otherwise(None)
            .alias("coverage_ratio")
        )
        .join(
            stock_basic.select(
                "symbol",
                pl.col("list_status").cast(pl.String),
                pl.col("name").cast(pl.String).alias("name"),
                pl.col("delist_date").cast(pl.String).alias("delist_date"),
            ),
            on="symbol",
            how="left",
        )
    )

    no_daily = coverage.filter(pl.col("actual_days") == 0)
    # Silent skip: still listed (L) per stock_basic, zero bars in-window, and
    # the gap is NOT explained by full-period suspension.
    silently_skipped = no_daily.filter(
        (pl.col("list_status") == "L") & (pl.col("missing_days") > 0)
    )
    gap_members = coverage.filter(
        (pl.col("expected_days") - pl.col("suspended_days") > 0)
        & (
            pl.col("coverage_ratio").is_null()
            | (pl.col("coverage_ratio") < COVERAGE_GAP_THRESHOLD)
        )
    ).sort("coverage_ratio")

    totals = {
        "expected_member_days": int(coverage["expected_days"].sum()),
        "actual_member_days": int(coverage["actual_days"].sum()),
        "suspended_member_days": int(coverage["suspended_days"].sum()),
        "missing_member_days": int(coverage["missing_days"].sum()),
    }
    totals["adjusted_coverage"] = (
        round(
            totals["actual_member_days"]
            / max(totals["expected_member_days"] - totals["suspended_member_days"], 1),
            6,
        )
    )

    # --- point-in-time member-count distribution across event dates.
    # A member leaves on member_end + 1 day (member_end is still inclusive).
    events: dict[str, int] = {}
    for start, end in membership.select("member_start", "member_end").iter_rows():
        events[str(start)] = events.get(str(start), 0) + 1
        leave = str(end + timedelta(days=1))
        events[leave] = events.get(leave, 0) - 1
    ordered_events = sorted(events)
    count = 0
    count_distribution: dict[str, int] = {}
    abnormal_periods: list[dict[str, Any]] = []
    for index, day in enumerate(ordered_events):
        count += events[day]
        next_day = ordered_events[index + 1] if index + 1 < len(ordered_events) else None
        if next_day is None:
            break  # trailing window after the final interval closes
        count_distribution[str(count)] = count_distribution.get(str(count), 0) + 1
        if count != expected_member_count:
            abnormal_periods.append(
                {"from": day, "until": next_day, "member_count": count}
            )

    fail_reasons: list[str] = []
    if len(silently_skipped) > MAX_SILENTLY_SKIPPED:
        fail_reasons.append(
            f"{len(silently_skipped)} still-listed members have zero daily bars"
        )
    if len(gap_members) > MAX_GAP_MEMBERS:
        fail_reasons.append(
            f"{len(gap_members)} members below coverage threshold "
            f"{COVERAGE_GAP_THRESHOLD}"
        )
    if abnormal_periods:
        fail_reasons.append(
            f"{len(abnormal_periods)} periods where member count != 800"
        )

    def _rows(frame: pl.DataFrame) -> list[dict[str, Any]]:
        return [
            {
                key: (str(value) if value is not None else None)
                for key, value in row.items()
            }
            for row in frame.iter_rows(named=True)
        ]

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "data_window": {
            "since": since,
            "first_calendar_day": calendar_days[0] if calendar_days else None,
            "last_calendar_day": calendar_days[-1] if calendar_days else None,
            "trading_days": len(calendar_days),
        },
        "stock_basic_status_counts": status_counts,
        "historical_constituents": {
            "total_since_2018": len(member_symbols),
            "with_daily_data": int(coverage.filter(pl.col("actual_days") > 0).height),
            "without_daily_data": _rows(no_daily),
        },
        "coverage": totals,
        "silently_skipped_count": int(silently_skipped.height),
        "silently_skipped": _rows(silently_skipped),
        "gap_members": _rows(gap_members),
        "point_in_time_member_count": {
            "distribution": count_distribution,
            "abnormal_periods": abnormal_periods,
        },
        "fail_conditions": {
            "coverage_gap_threshold": COVERAGE_GAP_THRESHOLD,
            "max_gap_members": MAX_GAP_MEMBERS,
            "max_silently_skipped": MAX_SILENTLY_SKIPPED,
            "expected_member_count": expected_member_count,
            "fail_reasons": fail_reasons,
        },
        "passed": not fail_reasons,
    }


def render_text_report(report: dict[str, Any]) -> str:
    window = report["data_window"]
    totals = report["coverage"]
    counts = report["stock_basic_status_counts"]
    hist = report["historical_constituents"]
    lines = [
        "# Historical Universe Coverage Audit",
        "",
        f"- window: {window['first_calendar_day']} .. {window['last_calendar_day']} "
        f"({window['trading_days']} trading days)",
        f"- stock_basic list_status: L={counts.get('L', 0)} "
        f"D={counts.get('D', 0)} P={counts.get('P', 0)}",
        f"- historical constituents since 2018: {hist['total_since_2018']}",
        f"- with daily data: {hist['with_daily_data']}",
        f"- without daily data: {len(hist['without_daily_data'])}",
        f"- member-days expected: {totals['expected_member_days']}",
        f"- member-days with bars: {totals['actual_member_days']}",
        f"- member-days suspended: {totals['suspended_member_days']}",
        f"- member-days missing: {totals['missing_member_days']}",
        f"- adjusted coverage: {totals['adjusted_coverage']}",
        f"- silently skipped (listed but zero bars): {report['silently_skipped_count']}",
        f"- point-in-time member-count distribution: "
        f"{report['point_in_time_member_count']['distribution']}",
        f"- abnormal periods (count != {report['fail_conditions']['expected_member_count']}): "
        f"{len(report['point_in_time_member_count']['abnormal_periods'])}",
        "",
    ]
    if report["fail_conditions"]["fail_reasons"]:
        lines.append("FAIL:")
        lines.extend(f"  - {r}" for r in report["fail_conditions"]["fail_reasons"])
    else:
        lines.append("PASS")
    return "\n".join(lines) + "\n"


def write_coverage_report(
    data_root: Path | str,
    output_json: Path | str,
    output_text: Path | str | None = None,
    since: str = "2018-01-01",
    expected_member_count: int = 800,
) -> dict[str, Any]:
    report = build_historical_universe_coverage(
        data_root, since=since, expected_member_count=expected_member_count
    )
    out = Path(output_json)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", "utf-8")
    if output_text is not None:
        text = Path(output_text)
        text.parent.mkdir(parents=True, exist_ok=True)
        text.write_text(render_text_report(report), "utf-8")
    return report
