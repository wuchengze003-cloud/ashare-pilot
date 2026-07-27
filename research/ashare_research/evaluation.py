"""System-generated signal and portfolio evaluation for model artifacts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import polars as pl
from sklearn.metrics import ndcg_score

from .portfolio import PortfolioConfig, PortfolioResult, PredictionBar, simulate_portfolio
from .universe import filter_frame_to_universe


@dataclass(frozen=True)
class SignalMetrics:
    rank_ic: float
    precision_at_k: float
    ndcg_at_k: float
    decision_days: int
    observations: int


@dataclass(frozen=True)
class EvaluationReport:
    schema_version: int
    evaluation_engine: str
    data_cutoff: str
    source_sha256: str
    universe_scope: str
    universe_sha256: str | None
    oos_folds: int
    signal: SignalMetrics
    portfolio: PortfolioResult
    double_cost_portfolio: PortfolioResult


def file_sha256(path: Path | str) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _as_date(value: object) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _signal_metrics(frame: pd.DataFrame, top_k: int) -> SignalMetrics:
    label_column = (
        "label_excess_return_5"
        if "label_excess_return_5" in frame.columns
        else "label_return_5"
    )
    valid = frame.dropna(subset=["prediction", label_column])
    rank_ics: list[float] = []
    precisions: list[float] = []
    ndcgs: list[float] = []
    for _, group in valid.groupby("date"):
        if len(group) < 2:
            continue
        prediction = group["prediction"].astype(float)
        label = group[label_column].astype(float)
        rank_ic = prediction.corr(label, method="spearman")
        if np.isfinite(rank_ic):
            rank_ics.append(float(rank_ic))
        selected = group.nlargest(min(top_k, len(group)), "prediction")
        precisions.append(float((selected[label_column] > 0).mean()))
        relevance = label.to_numpy() - float(label.min())
        if float(relevance.max()) > 0:
            ndcgs.append(
                float(
                    ndcg_score(
                        relevance.reshape(1, -1), prediction.to_numpy().reshape(1, -1), k=top_k
                    )
                )
            )
    return SignalMetrics(
        rank_ic=float(np.mean(rank_ics)) if rank_ics else 0.0,
        precision_at_k=float(np.mean(precisions)) if precisions else 0.0,
        ndcg_at_k=float(np.mean(ndcgs)) if ndcgs else 0.0,
        decision_days=int(valid["date"].nunique()),
        observations=len(valid),
    )


def _prediction_bars(frame: pl.DataFrame) -> list[PredictionBar]:
    required = {
        "date",
        "symbol",
        "prediction",
        "adj_close",
        "next_trade_date",
        "next_open",
        "next_close",
        "next_can_buy",
        "next_can_sell",
    }
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"evaluation frame missing columns: {sorted(missing)}")
    complete = frame.drop_nulls(
        ["prediction", "adj_close", "next_trade_date", "next_open", "next_close"]
    )
    return [
        PredictionBar(
            decision_date=_as_date(row["date"]),
            trade_date=_as_date(row["next_trade_date"]),
            symbol=str(row["symbol"]),
            score=float(row["prediction"]),
            close=float(row["adj_close"]),
            next_open=float(row["next_open"]),
            next_close=float(row["next_close"]),
            can_buy=bool(row["next_can_buy"]),
            can_sell=bool(row["next_can_sell"]),
        )
        for row in complete.iter_rows(named=True)
    ]


def evaluate_oos_predictions(
    prediction_path: Path | str,
    config: PortfolioConfig | None = None,
    universe_path: Path | str | None = None,
) -> EvaluationReport:
    prediction_path = Path(prediction_path)
    frame = pl.read_parquet(prediction_path)
    if universe_path is not None:
        frame = filter_frame_to_universe(frame, universe_path)
        if frame.is_empty():
            raise ValueError("point-in-time production universe has no evaluation rows")
    config = config or PortfolioConfig()
    bars = _prediction_bars(frame)
    portfolio = simulate_portfolio(bars, config)
    double_cost = simulate_portfolio(
        bars,
        PortfolioConfig(
            start_cash=config.start_cash,
            max_positions=config.max_positions,
            fee_bps=config.fee_bps * 2,
            min_expected_return=config.min_expected_return,
            switch_buffer=config.switch_buffer,
            rebalance_threshold_pct=config.rebalance_threshold_pct,
        ),
    )
    cutoff = str(frame["date"].max())
    folds = int(frame["fold"].n_unique()) if "fold" in frame.columns else 0
    return EvaluationReport(
        schema_version=2,
        evaluation_engine="ashare-next-open-v2",
        data_cutoff=cutoff,
        source_sha256=file_sha256(prediction_path),
        universe_scope=("production-ai-point-in-time" if universe_path else "full-a-share"),
        universe_sha256=(file_sha256(universe_path) if universe_path else None),
        oos_folds=folds,
        signal=_signal_metrics(frame.to_pandas(), config.max_positions),
        portfolio=portfolio,
        double_cost_portfolio=double_cost,
    )


def write_evaluation_report(report: EvaluationReport, output_path: Path | str) -> None:
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = asdict(report)
    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
        "utf-8",
    )
