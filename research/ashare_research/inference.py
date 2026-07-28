"""Produce shadow predictions without changing the production strategy."""

from __future__ import annotations

import json
import pickle
import tempfile
from datetime import UTC, date, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import polars as pl

from .contracts import HORIZONS, DecisionEvent, PredictionRecord
from .explain import local_feature_contributions, top_contributions
from .ledger import append_decision, append_prediction, init_ledger
from .shadow import advance_shadow_account, current_weights, set_pending_targets
from .universe import active_symbols_as_of


def _public_symbol(symbol: str) -> str:
    return symbol[2:] if symbol[:2] in {"sh", "sz", "bj"} else symbol


def _predict_model(model, features: pd.DataFrame) -> np.ndarray:
    if hasattr(model, "model") and model.model is not None:
        return np.asarray(model.model.predict(features.values), dtype=float)
    if hasattr(model, "coef_"):
        return np.asarray(features.values @ model.coef_ + model.intercept_, dtype=float)
    if hasattr(model, "ensemble"):
        predictions = [
            submodel.predict(features.loc[:, columns].values) * weight
            for submodel, columns, weight in zip(
                model.ensemble, model.sub_features, model.sub_weights, strict=False
            )
        ]
        return np.sum(predictions, axis=0) / np.sum(model.sub_weights)
    raise TypeError(f"unsupported trained model object: {type(model)!r}")


def generate_shadow_snapshot(
    panel_path: Path | str,
    model_bundle_path: Path | str,
    universe_path: Path | str,
    output_path: Path | str,
    ledger_path: Path | str,
    model_version: str,
    max_positions: int = 4,
    stage: str = "shadow",
    quality: dict | None = None,
    shadow_state_path: Path | str | None = None,
) -> dict:
    panel = pl.read_parquet(panel_path)
    decision_date = str(panel["date"].max())
    latest = panel.filter(pl.col("date") == panel["date"].max())
    active = active_symbols_as_of(Path(universe_path), decision_date)
    latest = latest.filter(pl.col("symbol").is_in(active))
    account = None
    weights: dict[str, float] = {}
    if shadow_state_path is not None:
        account = advance_shadow_account(
            shadow_state_path,
            panel,
            ledger_path,
            model_version,
            market_panel=panel,          # full panel for pricing (incl. exited stocks)
            active_symbols=active,        # force-sell stocks that left the universe
        )
        close_prices = {
            _public_symbol(row["symbol"]): float(row["close"])
            for row in latest.select("symbol", "close").iter_rows(named=True)
        }
        weights = current_weights(account, close_prices)
    with Path(model_bundle_path).open("rb") as handle:
        bundle = pickle.load(handle)
    features = list(bundle["features"])
    frame = latest.select(["symbol", *features]).to_pandas().set_index("symbol")
    frame = frame.replace([np.inf, -np.inf], np.nan).fillna(0.0)
    expected: dict[int, pd.Series] = {}
    for horizon in HORIZONS:
        expected[horizon] = pd.Series(
            _predict_model(bundle["models"][horizon], frame), index=frame.index
        )
    utility = 0.15 * expected[1] + 0.35 * expected[3] + 0.30 * expected[5] + 0.20 * expected[10]
    if bundle.get("downside_models", {}).get(5) is not None:
        downside = pd.Series(
            _predict_model(bundle["downside_models"][5], frame),
            index=frame.index,
        )
    else:
        downside = pd.Series(bundle["residual_downside"].get(5, 0.0), index=frame.index)
    score = utility + downside.clip(upper=0) * 0.5
    ranked = score.sort_values(ascending=False)
    positive = ranked[ranked > 0.003]
    initial = list(positive.head(max_positions).index)
    cutoff = float(positive.iloc[min(max_positions, len(positive)) - 1]) if len(positive) else 0.003
    retained = [
        symbol
        for symbol in positive.index
        if _public_symbol(symbol) in weights and float(positive[symbol]) >= cutoff - 0.002
    ][:max_positions]
    desired = list(retained)
    for symbol in initial:
        if len(desired) >= max_positions:
            break
        if symbol not in desired:
            desired.append(symbol)
    eligible = ranked.loc[desired]
    target_weight = 1 / max_positions if len(eligible) else 0.0
    percentile = score.rank(pct=True)
    contributions = local_feature_contributions(bundle["models"][5], frame)

    predictions = []
    init_ledger(ledger_path)
    for rank, (symbol, value) in enumerate(ranked.items(), 1):
        public_symbol = _public_symbol(symbol)
        was_held = weights.get(public_symbol, 0.0) > 0
        in_target = symbol in eligible.index
        action = "buy" if in_target and not was_held else "hold"
        if was_held and not in_target:
            action = "sell"
        target_after = target_weight if in_target else 0.0
        returns = {f"d{horizon}": float(expected[horizon][symbol]) for horizon in HORIZONS}
        record = {
            "symbol": public_symbol,
            "rank": rank,
            "score": float(value),
            "expectedReturns": returns,
            "downsideRisk": float(downside[symbol]),
            "confidence": float(percentile[symbol]),
            "targetWeight": target_after,
            "action": action,
            "reasonCodes": (
                ["ENTER_POSITIVE_UTILITY"]
                if action == "buy"
                else ["EXIT_UTILITY_LOST"]
                if action == "sell"
                else ["RETAIN_POSITIVE_UTILITY"]
                if in_target
                else ["NO_POSITION_NO_TRADE"]
            ),
            "featureContributions": top_contributions(contributions, symbol),
        }
        predictions.append(record)
        append_decision(
            ledger_path,
            DecisionEvent(
                decision_date=date.fromisoformat(decision_date),
                model_version=model_version,
                symbol=public_symbol,
                action=action,
                rank=rank,
                target_weight_before=weights.get(public_symbol, 0.0),
                target_weight_after=target_after,
                expected_return=float(value),
                downside_risk=float(downside[symbol]),
                reason_codes=tuple(record["reasonCodes"]),
            ),
        )
        for horizon in HORIZONS:
            append_prediction(
                ledger_path,
                PredictionRecord(
                    decision_date=date.fromisoformat(decision_date),
                    data_cutoff=date.fromisoformat(decision_date),
                    symbol=public_symbol,
                    model_version=model_version,
                    feature_version=bundle["feature_version"],
                    horizon_bars=horizon,
                    expected_return=returns[f"d{horizon}"],
                    downside_return=float(downside[symbol]),
                    confidence=float(percentile[symbol]),
                    rank=rank,
                    target_weight=target_after,
                    action=action,
                    reason_codes=tuple(record["reasonCodes"]),
                ),
            )

    snapshot = {
        "generated_at": datetime.now(UTC).isoformat(),
        "decision_date": decision_date,
        "data_cutoff": decision_date,
        "stage": stage,
        "model_version": model_version,
        "feature_version": bundle["feature_version"],
        "source": "qlib",
        "predictions": predictions,
        "quality": quality
        or {
            "data_quality_passed": False,
            "drift_passed": False,
            "warnings": ["quality and drift reports were not provided"],
        },
        "shadow_account": (
            {
                "cash": account["cash"],
                "positions": account["positions"],
                "equity_curve": account["equity_curve"],
            }
            if account
            else None
        ),
    }
    if account is not None and shadow_state_path is not None:
        set_pending_targets(
            shadow_state_path,
            account,
            decision_date,
            {
                prediction["symbol"]: prediction["targetWeight"]
                for prediction in predictions
                if prediction["targetWeight"] > 0
            },
        )
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=output_path.parent, delete=False
    ) as handle:
        json.dump(snapshot, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        temporary = handle.name
    Path(temporary).replace(output_path)
    return snapshot
