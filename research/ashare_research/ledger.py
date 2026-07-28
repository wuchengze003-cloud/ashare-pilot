"""Append-only prediction, decision, execution and outcome ledger."""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import asdict
from datetime import date
from pathlib import Path
from statistics import median

from .contracts import DecisionEvent, ExecutionEvent, OutcomeRecord, PredictionRecord, PriceBar

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS predictions (
  decision_date TEXT NOT NULL,
  data_cutoff TEXT NOT NULL,
  symbol TEXT NOT NULL,
  model_version TEXT NOT NULL,
  feature_version TEXT NOT NULL,
  horizon_bars INTEGER NOT NULL,
  expected_return REAL NOT NULL,
  downside_return REAL NOT NULL,
  confidence REAL NOT NULL,
  rank_value INTEGER NOT NULL,
  target_weight REAL NOT NULL,
  action TEXT NOT NULL,
  reason_codes TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (decision_date, symbol, model_version, horizon_bars)
);
CREATE TABLE IF NOT EXISTS decisions (
  decision_date TEXT NOT NULL,
  model_version TEXT NOT NULL,
  symbol TEXT NOT NULL,
  payload TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (decision_date, model_version, symbol)
);
CREATE TABLE IF NOT EXISTS executions (
  decision_date TEXT NOT NULL,
  trade_date TEXT NOT NULL,
  model_version TEXT NOT NULL,
  symbol TEXT NOT NULL,
  side TEXT NOT NULL,
  payload TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (decision_date, trade_date, model_version, symbol, side)
);
CREATE TABLE IF NOT EXISTS outcomes (
  decision_date TEXT NOT NULL,
  symbol TEXT NOT NULL,
  model_version TEXT NOT NULL,
  horizon_bars INTEGER NOT NULL,
  payload TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (decision_date, symbol, model_version, horizon_bars)
);
"""


def connect(db_path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(Path(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def init_ledger(db_path: Path | str) -> None:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with connect(path) as conn:
        conn.executescript(SCHEMA)


def append_prediction(db_path: Path | str, record: PredictionRecord) -> bool:
    with connect(db_path) as conn:
        result = conn.execute(
            """INSERT OR IGNORE INTO predictions VALUES
            (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
            (
                record.decision_date.isoformat(),
                record.data_cutoff.isoformat(),
                record.symbol,
                record.model_version,
                record.feature_version,
                record.horizon_bars,
                record.expected_return,
                record.downside_return,
                record.confidence,
                record.rank,
                record.target_weight,
                record.action,
                json.dumps(record.reason_codes),
            ),
        )
        return result.rowcount == 1


def append_decision(db_path: Path | str, event: DecisionEvent) -> bool:
    with connect(db_path) as conn:
        result = conn.execute(
            "INSERT OR IGNORE INTO decisions VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)",
            (
                event.decision_date.isoformat(),
                event.model_version,
                event.symbol,
                json.dumps(asdict(event), default=str),
            ),
        )
        return result.rowcount == 1


def append_execution(db_path: Path | str, event: ExecutionEvent) -> bool:
    with connect(db_path) as conn:
        result = conn.execute(
            "INSERT OR IGNORE INTO executions VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
            (
                event.decision_date.isoformat(),
                event.trade_date.isoformat(),
                event.model_version,
                event.symbol,
                event.side,
                json.dumps(asdict(event), default=str),
            ),
        )
        return result.rowcount == 1


def append_outcome(db_path: Path | str, outcome: OutcomeRecord) -> bool:
    with connect(db_path) as conn:
        result = conn.execute(
            "INSERT OR IGNORE INTO outcomes VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
            (
                outcome.decision_date.isoformat(),
                outcome.symbol,
                outcome.model_version,
                outcome.horizon_bars,
                json.dumps(asdict(outcome), default=str),
            ),
        )
        return result.rowcount == 1


def read_predictions(db_path: Path | str, as_of: date | None = None) -> list[PredictionRecord]:
    sql = "SELECT * FROM predictions"
    params: tuple[str, ...] = ()
    if as_of is not None:
        sql += " WHERE decision_date <= ? AND data_cutoff <= ?"
        params = (as_of.isoformat(), as_of.isoformat())
    sql += " ORDER BY decision_date, rank_value, symbol, horizon_bars"
    with connect(db_path) as conn:
        rows = conn.execute(sql, params).fetchall()
    return [
        PredictionRecord(
            decision_date=date.fromisoformat(row["decision_date"]),
            data_cutoff=date.fromisoformat(row["data_cutoff"]),
            symbol=row["symbol"],
            model_version=row["model_version"],
            feature_version=row["feature_version"],
            horizon_bars=row["horizon_bars"],
            expected_return=row["expected_return"],
            downside_return=row["downside_return"],
            confidence=row["confidence"],
            rank=row["rank_value"],
            target_weight=row["target_weight"],
            action=row["action"],
            reason_codes=tuple(json.loads(row["reason_codes"])),
        )
        for row in rows
    ]


def read_outcomes(db_path: Path | str) -> list[OutcomeRecord]:
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT payload FROM outcomes ORDER BY decision_date, symbol, horizon_bars"
        ).fetchall()
    records = []
    for row in rows:
        item = json.loads(row["payload"])
        for key in ("decision_date", "entry_date", "evaluation_date"):
            item[key] = date.fromisoformat(item[key])
        records.append(OutcomeRecord(**item))
    return records


def read_executions(
    db_path: Path | str,
    model_version: str | None = None,
) -> list[ExecutionEvent]:
    sql = "SELECT payload FROM executions"
    params: tuple[str, ...] = ()
    if model_version:
        sql += " WHERE model_version = ?"
        params = (model_version,)
    sql += " ORDER BY trade_date, symbol, side"
    with connect(db_path) as conn:
        rows = conn.execute(sql, params).fetchall()
    records = []
    for row in rows:
        item = json.loads(row["payload"])
        item["decision_date"] = date.fromisoformat(item["decision_date"])
        item["trade_date"] = date.fromisoformat(item["trade_date"])
        records.append(ExecutionEvent(**item))
    return records


def backfill_outcomes(
    db_path: Path | str,
    bars: Iterable[PriceBar],
    as_of_date: date,
    benchmark_symbol: str | None = None,
    round_trip_fee_bps: float = 16.0,  # from config/cost-model.json
) -> int:
    """Evaluate D-close predictions from D+1 open without future leakage."""
    by_symbol: dict[str, list[PriceBar]] = defaultdict(list)
    for bar in bars:
        by_symbol[bar.symbol].append(bar)
    for rows in by_symbol.values():
        rows.sort(key=lambda row: row.date)

    pending: list[OutcomeRecord] = []
    for prediction in read_predictions(db_path, as_of=as_of_date):
        future = [
            bar
            for bar in by_symbol.get(prediction.symbol, [])
            if bar.date > prediction.decision_date
        ]
        if len(future) < prediction.horizon_bars:
            continue
        entry = future[0]
        evaluation = future[prediction.horizon_bars - 1]
        if evaluation.date > as_of_date or entry.open <= 0:
            continue
        path = future[: prediction.horizon_bars]
        gross_return = evaluation.close / entry.open - 1
        net_return = gross_return - round_trip_fee_bps / 10_000
        benchmark_return: float | None = None
        if benchmark_symbol:
            benchmark = {bar.date: bar for bar in by_symbol.get(benchmark_symbol, [])}
            bench_entry = benchmark.get(entry.date)
            bench_exit = benchmark.get(evaluation.date)
            if bench_entry and bench_exit and bench_entry.open > 0:
                benchmark_return = bench_exit.close / bench_entry.open - 1
        pending.append(
            OutcomeRecord(
                decision_date=prediction.decision_date,
                symbol=prediction.symbol,
                model_version=prediction.model_version,
                horizon_bars=prediction.horizon_bars,
                entry_date=entry.date,
                entry_open=entry.open,
                evaluation_date=evaluation.date,
                exit_close=evaluation.close,
                net_return=net_return,
                benchmark_return=benchmark_return,
                excess_return=None if benchmark_return is None else net_return - benchmark_return,
                mfe=max(bar.high / entry.open - 1 for bar in path),
                mae=min(bar.low / entry.open - 1 for bar in path),
                opportunity_cost=0,
            )
        )

    returns_by_group: dict[tuple[date, str, int], list[float]] = defaultdict(list)
    best_by_group: dict[tuple[date, str, int], float] = {}
    for item in pending:
        key = (item.decision_date, item.model_version, item.horizon_bars)
        returns_by_group[key].append(item.net_return)
        best_by_group[key] = max(best_by_group.get(key, item.net_return), item.net_return)

    inserted = 0
    for item in pending:
        key = (item.decision_date, item.model_version, item.horizon_bars)
        benchmark_return = (
            item.benchmark_return
            if item.benchmark_return is not None
            else median(returns_by_group[key])
        )
        enriched = OutcomeRecord(
            **{
                **asdict(item),
                "benchmark_return": benchmark_return,
                "excess_return": item.net_return - benchmark_return,
                "opportunity_cost": best_by_group[key] - item.net_return,
            }
        )
        inserted += int(append_outcome(db_path, enriched))
    return inserted


def ledger_counts(db_path: Path | str) -> dict[str, int]:
    with connect(db_path) as conn:
        return {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("predictions", "decisions", "executions", "outcomes")
        }


def summarize_outcomes(db_path: Path | str) -> dict:
    predictions = {
        (
            item.decision_date,
            item.symbol,
            item.model_version,
            item.horizon_bars,
        ): item
        for item in read_predictions(db_path)
    }
    grouped: dict[tuple[str, int], list[tuple[PredictionRecord, OutcomeRecord]]] = defaultdict(
        list
    )
    for outcome in read_outcomes(db_path):
        key = (
            outcome.decision_date,
            outcome.symbol,
            outcome.model_version,
            outcome.horizon_bars,
        )
        prediction = predictions.get(key)
        if prediction:
            grouped[(outcome.model_version, outcome.horizon_bars)].append(
                (prediction, outcome)
            )
    items = []
    for (model_version, horizon), rows in sorted(grouped.items()):
        count = len(rows)
        items.append(
            {
                "model_version": model_version,
                "horizon_bars": horizon,
                "observations": count,
                "hit_rate": sum(
                    (outcome.excess_return or 0) > 0 for _, outcome in rows
                )
                / count,
                "net_hit_rate": sum(outcome.net_return > 0 for _, outcome in rows) / count,
                "mean_net_return": sum(outcome.net_return for _, outcome in rows) / count,
                "mean_excess_return": (
                    sum(outcome.excess_return for _, outcome in rows if outcome.excess_return is not None)
                    / max(1, sum(outcome.excess_return is not None for _, outcome in rows))
                ),
                "mean_mfe": sum(outcome.mfe for _, outcome in rows) / count,
                "mean_mae": sum(outcome.mae for _, outcome in rows) / count,
                "mean_opportunity_cost": sum(outcome.opportunity_cost for _, outcome in rows)
                / count,
                "mean_absolute_calibration_error": sum(
                    abs(
                        prediction.expected_return
                        - (
                            outcome.excess_return
                            if outcome.excess_return is not None
                            else outcome.net_return
                        )
                    )
                    for prediction, outcome in rows
                )
                / count,
            }
        )
    return {"groups": items, "outcomes": sum(item["observations"] for item in items)}
