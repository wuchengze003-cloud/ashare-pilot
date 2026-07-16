"""Point-in-time adapter for the curated AI production universe."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import polars as pl


def prefixed_symbol(code: str) -> str:
    prefix = "sh" if code.startswith(("6", "9")) else "bj" if code.startswith(("4", "8")) else "sz"
    return f"{prefix}{code}"


def _participates(entry: dict) -> bool:
    has_range = bool(entry.get("strategy_from") or entry.get("strategy_until"))
    return entry.get("pool_tier") != "watch" or has_range


def load_universe_intervals(path: Path | str) -> pl.DataFrame:
    entries = json.loads(Path(path).read_text("utf-8"))["entries"]
    rows = [
        {
            "symbol": prefixed_symbol(entry["symbol"]),
            "strategy_from": date.fromisoformat(entry.get("strategy_from", "1900-01-01")),
            "strategy_until": date.fromisoformat(entry.get("strategy_until", "2999-12-31")),
        }
        for entry in entries
        if _participates(entry)
    ]
    return pl.DataFrame(
        rows,
        schema={"symbol": pl.String, "strategy_from": pl.Date, "strategy_until": pl.Date},
    )


def active_symbols_as_of(path: Path | str, decision_date: str) -> set[str]:
    value = date.fromisoformat(decision_date)
    intervals = load_universe_intervals(path)
    return set(
        intervals.filter(
            (pl.col("strategy_from") <= value) & (pl.col("strategy_until") >= value)
        )["symbol"].to_list()
    )


def filter_frame_to_universe(frame: pl.DataFrame, path: Path | str) -> pl.DataFrame:
    intervals = load_universe_intervals(path)
    date_column = frame.schema["date"]
    working = frame.with_columns(
        pl.col("date").cast(pl.Date).alias("__membership_date")
        if date_column != pl.Date
        else pl.col("date").alias("__membership_date")
    )
    return (
        working.join(intervals, on="symbol", how="inner")
        .filter(
            (pl.col("__membership_date") >= pl.col("strategy_from"))
            & (pl.col("__membership_date") <= pl.col("strategy_until"))
        )
        .drop("__membership_date", "strategy_from", "strategy_until")
    )
