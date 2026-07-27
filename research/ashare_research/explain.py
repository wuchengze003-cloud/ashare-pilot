"""Native explainability adapters for Qlib linear and LightGBM models."""

from __future__ import annotations

import numpy as np
import pandas as pd


def global_feature_importance(model, feature_names: list[str]) -> dict[str, float]:
    values = None
    booster = getattr(model, "model", None)
    if booster is not None and hasattr(booster, "feature_importance"):
        values = np.asarray(booster.feature_importance(importance_type="gain"), dtype=float)
    elif getattr(model, "coef_", None) is not None:
        values = np.abs(np.asarray(model.coef_, dtype=float))
    if values is None or len(values) != len(feature_names):
        return {}
    total = float(np.abs(values).sum())
    if total <= 0:
        return {name: 0.0 for name in feature_names}
    return {
        name: float(value / total)
        for name, value in sorted(
            zip(feature_names, np.abs(values), strict=True),
            key=lambda item: (-item[1], item[0]),
        )
    }


def local_feature_contributions(model, frame: pd.DataFrame) -> pd.DataFrame:
    booster = getattr(model, "model", None)
    if booster is not None and hasattr(booster, "predict"):
        try:
            values = np.asarray(booster.predict(frame.values, pred_contrib=True), dtype=float)
            if values.shape == (len(frame), len(frame.columns) + 1):
                return pd.DataFrame(values[:, :-1], index=frame.index, columns=frame.columns)
        except TypeError:
            pass
    coefficient = getattr(model, "coef_", None)
    if coefficient is not None and len(coefficient) == len(frame.columns):
        return frame.mul(np.asarray(coefficient, dtype=float), axis=1)
    return pd.DataFrame(0.0, index=frame.index, columns=frame.columns)


def top_contributions(
    contributions: pd.DataFrame,
    symbol: str,
    limit: int = 3,
) -> dict[str, float]:
    if symbol not in contributions.index:
        return {}
    row = contributions.loc[symbol]
    ranked = sorted(row.items(), key=lambda item: (-abs(float(item[1])), item[0]))[:limit]
    return {name: float(value) for name, value in ranked}
