"""Qlib model training and purged walk-forward evaluation."""

from __future__ import annotations

import json
import pickle
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Literal

import numpy as np
import pandas as pd
import polars as pl

from .contracts import HORIZONS
from .data_sync import assert_production_dataset
from .explain import global_feature_importance
from .features import FEATURE_VERSION
from .qlib_config import sqlite_exp_manager


@dataclass(frozen=True)
class WalkForwardFold:
    train: tuple[str, str]
    valid: tuple[str, str]
    test: tuple[str, str]


@dataclass(frozen=True)
class TrainingResult:
    model_version: str
    model_type: str
    feature_version: str
    data_cutoff: str
    artifact_path: str
    oos_prediction_path: str
    holdout_prediction_path: str
    oos_folds: int
    fold_rank_ic: tuple[float, ...]
    median_rank_ic: float
    parameters: dict
    data_source: str
    promotable: bool
    training_windows: tuple[dict[str, tuple[str, str]], ...]


def make_walk_forward_folds(
    dates: list[date],
    holdout_start: date = date(2026, 2, 24),
    train_bars: int = 252 * 5,
    valid_bars: int = 126,
    test_bars: int = 63,
    purge_bars: int = 10,
    minimum_folds: int = 6,
) -> list[WalkForwardFold]:
    eligible = sorted({value for value in dates if value < holdout_start})
    first_test = train_bars + valid_bars + purge_bars * 2
    folds: list[WalkForwardFold] = []
    for test_start in range(first_test, len(eligible), test_bars):
        test_end = min(test_start + test_bars - 1, len(eligible) - 1)
        if test_end - test_start + 1 < test_bars:
            break
        valid_end = test_start - purge_bars - 1
        valid_start = valid_end - valid_bars + 1
        train_end = valid_start - purge_bars - 1
        train_start = train_end - train_bars + 1
        if train_start < 0:
            continue
        folds.append(
            WalkForwardFold(
                train=(eligible[train_start].isoformat(), eligible[train_end].isoformat()),
                valid=(eligible[valid_start].isoformat(), eligible[valid_end].isoformat()),
                test=(eligible[test_start].isoformat(), eligible[test_end].isoformat()),
            )
        )
    if len(folds) < minimum_folds:
        raise ValueError(f"only {len(folds)} walk-forward folds; require {minimum_folds}")
    return folds


def make_frozen_holdout_fold(
    dates: list[date],
    holdout_start: date = date(2026, 2, 24),
    train_bars: int = 252 * 5,
    valid_bars: int = 126,
    purge_bars: int = 10,
) -> WalkForwardFold:
    ordered = sorted(set(dates))
    try:
        holdout_start_index = next(
            index for index, value in enumerate(ordered) if value >= holdout_start
        )
    except StopIteration as error:
        raise ValueError("panel does not contain the post-CNY 2026 holdout") from error
    valid_end = holdout_start_index - purge_bars - 1
    valid_start = valid_end - valid_bars + 1
    train_end = valid_start - purge_bars - 1
    train_start = train_end - train_bars + 1
    if train_start < 0:
        raise ValueError("insufficient pre-holdout history for frozen 2026 evaluation")
    return WalkForwardFold(
        train=(ordered[train_start].isoformat(), ordered[train_end].isoformat()),
        valid=(ordered[valid_start].isoformat(), ordered[valid_end].isoformat()),
        test=(ordered[holdout_start_index].isoformat(), ordered[-1].isoformat()),
    )


def _qlib_frame(panel: pl.DataFrame, features: list[str], label: str) -> pd.DataFrame:
    required = ["date", "symbol", *features, label]
    frame = panel.select(required).to_pandas()
    frame["date"] = pd.to_datetime(frame["date"])
    frame = frame.set_index(["date", "symbol"]).sort_index()
    frame.index.names = ["datetime", "instrument"]
    feature_frame = frame[features]
    label_frame = frame[[label]].rename(columns={label: "LABEL0"})
    return pd.concat({"feature": feature_frame, "label": label_frame}, axis=1)


def _dataset(frame: pd.DataFrame, fold: WalkForwardFold):
    from qlib.data.dataset import DatasetH
    from qlib.data.dataset.handler import DataHandlerLP
    from qlib.data.dataset.loader import StaticDataLoader

    loader = StaticDataLoader(frame)
    handler = DataHandlerLP(
        data_loader=loader,
        infer_processors=[{"class": "ProcessInf"}, {"class": "Fillna"}],
        learn_processors=[{"class": "DropnaLabel"}],
    )
    return DatasetH(
        handler=handler, segments={"train": fold.train, "valid": fold.valid, "test": fold.test}
    )


def _model(model_type: str, params: dict):
    if model_type == "linear":
        from qlib.contrib.model.linear import LinearModel

        return LinearModel(
            estimator="ridge", alpha=float(params.get("alpha", 1.0)), fit_intercept=True
        )
    if model_type == "lightgbm":
        from qlib.contrib.model.gbdt import LGBModel

        return LGBModel(loss="mse", **params)
    if model_type == "double_ensemble":
        from qlib.contrib.model.double_ensemble import DEnsembleModel

        return DEnsembleModel(loss="mse", **params)
    raise ValueError(f"unsupported model type: {model_type}")


def _rank_ic(prediction: pd.Series, label: pd.Series) -> float:
    joined = pd.concat({"prediction": prediction, "label": label}, axis=1).dropna()
    if joined.empty:
        return float("nan")
    values = joined.groupby(level="datetime").apply(
        lambda group: group["prediction"].corr(group["label"], method="spearman"),
        include_groups=False,
    )
    return float(values.replace([np.inf, -np.inf], np.nan).dropna().mean())


def _evaluate_segment(
    frame: pd.DataFrame,
    fold: WalkForwardFold,
    model_type: str,
    params: dict,
    segment: Literal["valid", "test"],
) -> tuple[float, pd.Series]:
    from qlib.data.dataset.handler import DataHandlerLP
    from qlib.workflow import R

    dataset = _dataset(frame, fold)
    model = _model(model_type, params)
    with R.start(experiment_name="ashare-walk-forward"):
        model.fit(dataset)
    prediction = model.predict(dataset, segment)
    label = dataset.prepare(segment, col_set="label", data_key=DataHandlerLP.DK_L).iloc[:, 0]
    return _rank_ic(prediction, label), prediction


def _evaluate_fold(
    frame: pd.DataFrame,
    fold: WalkForwardFold,
    model_type: str,
    params: dict,
) -> tuple[float, pd.Series]:
    return _evaluate_segment(frame, fold, model_type, params, "test")


def _default_params(model_type: str) -> dict:
    if model_type == "linear":
        return {"alpha": 1.0}
    if model_type == "double_ensemble":
        return {"num_models": 4, "epochs": 100, "early_stopping_rounds": 20, "verbosity": -1}
    return {
        "learning_rate": 0.03,
        "num_leaves": 31,
        "max_depth": 8,
        "subsample": 0.8,
        "colsample_bytree": 0.8,
        "lambda_l1": 1.0,
        "lambda_l2": 5.0,
        "num_boost_round": 500,
        "early_stopping_rounds": 50,
        "num_threads": 8,
    }


def _optimize_lightgbm(frame: pd.DataFrame, folds: list[WalkForwardFold], trials: int) -> dict:
    import optuna

    evaluation_folds = folds[-3:]

    def objective(trial: optuna.Trial) -> tuple[float, float]:
        params = {
            "learning_rate": trial.suggest_float("learning_rate", 0.01, 0.08, log=True),
            "num_leaves": trial.suggest_int("num_leaves", 15, 63),
            "max_depth": trial.suggest_int("max_depth", 4, 10),
            "subsample": trial.suggest_float("subsample", 0.65, 1.0),
            "colsample_bytree": trial.suggest_float("colsample_bytree", 0.65, 1.0),
            "lambda_l1": trial.suggest_float("lambda_l1", 1e-3, 20, log=True),
            "lambda_l2": trial.suggest_float("lambda_l2", 1e-3, 50, log=True),
            "num_boost_round": 500,
            "early_stopping_rounds": 50,
            "num_threads": 8,
        }
        scores = [
            _evaluate_segment(frame, fold, "lightgbm", params, "valid")[0]
            for fold in evaluation_folds
        ]
        median = float(np.nanmedian(scores))
        dispersion = float(np.nanstd(scores))
        if not np.isfinite(median) or not np.isfinite(dispersion):
            raise optuna.TrialPruned("non-finite walk-forward score")
        return median, dispersion

    study = optuna.create_study(directions=["maximize", "minimize"])
    study.optimize(objective, n_trials=min(max(trials, 1), 30), show_progress_bar=False)
    if not study.best_trials:
        raise RuntimeError("Optuna produced no valid Pareto candidates")
    best = max(study.best_trials, key=lambda trial: trial.values[0] - trial.values[1])
    return {
        **best.params,
        "num_boost_round": 500,
        "early_stopping_rounds": 50,
        "num_threads": 8,
    }


def train_models(
    panel_path: Path | str,
    artifact_root: Path | str,
    model_type: Literal["linear", "lightgbm", "double_ensemble"] = "lightgbm",
    optuna_trials: int = 20,
) -> TrainingResult:
    import qlib
    from qlib.workflow import R

    panel_path = Path(panel_path)
    artifact_root = Path(artifact_root)
    manifest = json.loads(panel_path.with_suffix(".manifest.json").read_text("utf-8"))
    features = list(manifest["features"])
    panel = pl.read_parquet(panel_path)
    assert_production_dataset(
        artifact_root / "data",
        str(panel["date"].min()),
        str(panel["date"].max()),
    )
    folds = make_walk_forward_folds(panel["date"].to_list())
    qlib.init(
        provider_uri=None,
        exp_manager=sqlite_exp_manager(artifact_root / "mlflow.db"),
    )

    tuning_frame = _qlib_frame(panel, features, "label_excess_return_5")
    params = _default_params(model_type)
    if model_type == "lightgbm" and optuna_trials:
        params = _optimize_lightgbm(tuning_frame, folds, optuna_trials)

    fold_results = [_evaluate_fold(tuning_frame, fold, model_type, params) for fold in folds]
    fold_scores = tuple(result[0] for result in fold_results)
    bundle: dict[str, object] = {
        "model_type": model_type,
        "feature_version": FEATURE_VERSION,
        "features": features,
        "parameters": params,
        "models": {},
        "downside_models": {},
        "residual_downside": {},
        "prediction_units": "net-excess-return-fraction",
    }
    all_dates = sorted(set(panel["date"].to_list()))
    holdout_fold = make_frozen_holdout_fold(all_dates)
    latest_index = len(all_dates) - 1
    valid_end_index = latest_index - max(HORIZONS) - 1
    valid_start_index = valid_end_index - 126 + 1
    train_end_index = valid_start_index - 10 - 1
    train_start_index = max(0, train_end_index - 252 * 5 + 1)
    if train_end_index <= train_start_index or valid_end_index <= valid_start_index:
        raise ValueError("insufficient data for final purged train/validation split")
    final_fold = WalkForwardFold(
        train=(all_dates[train_start_index].isoformat(), all_dates[train_end_index].isoformat()),
        valid=(all_dates[valid_start_index].isoformat(), all_dates[valid_end_index].isoformat()),
        test=(all_dates[latest_index].isoformat(), all_dates[latest_index].isoformat()),
    )
    for horizon in HORIZONS:
        frame = _qlib_frame(panel, features, f"label_excess_return_{horizon}")
        dataset = _dataset(frame, final_fold)
        model = _model(model_type, params)
        with R.start(experiment_name=f"ashare-final-h{horizon}"):
            model.fit(dataset)
        validation_prediction = model.predict(dataset, "valid")
        from qlib.data.dataset.handler import DataHandlerLP

        validation_label = dataset.prepare(
            "valid", col_set="label", data_key=DataHandlerLP.DK_L
        ).iloc[:, 0]
        residual = (validation_label - validation_prediction).dropna()
        bundle["models"][horizon] = model  # type: ignore[index]
        bundle["residual_downside"][horizon] = (
            float(residual.quantile(0.1)) if not residual.empty else 0.0
        )  # type: ignore[index]
        downside_frame = _qlib_frame(panel, features, f"label_downside_{horizon}")
        downside_dataset = _dataset(downside_frame, final_fold)
        downside_model = _model(model_type, params)
        with R.start(experiment_name=f"ashare-downside-h{horizon}"):
            downside_model.fit(downside_dataset)
        bundle["downside_models"][horizon] = downside_model  # type: ignore[index]

    cutoff = str(panel["date"].max())
    model_version = f"{model_type}-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
    output_dir = artifact_root / "models" / model_version
    output_dir.mkdir(parents=True, exist_ok=False)
    artifact_path = output_dir / "model-bundle.pkl"
    with artifact_path.open("wb") as handle:
        pickle.dump(bundle, handle)
    importance = {
        "feature_version": FEATURE_VERSION,
        "model_version": model_version,
        "return_models": {
            f"d{horizon}": global_feature_importance(
                bundle["models"][horizon], features  # type: ignore[index]
            )
            for horizon in HORIZONS
        },
        "downside_models": {
            f"d{horizon}": global_feature_importance(
                bundle["downside_models"][horizon], features  # type: ignore[index]
            )
            for horizon in HORIZONS
        },
    }
    (output_dir / "feature-importance.json").write_text(
        json.dumps(importance, ensure_ascii=False, indent=2) + "\n",
        "utf-8",
    )
    execution_columns = [
        "date",
        "symbol",
        "close",
        "adj_close",
        "next_trade_date",
        "next_raw_open",
        "next_raw_close",
        "next_open",
        "next_close",
        "next_can_buy",
        "next_can_sell",
        "amount",
        "volatility_20",
        "label_return_5",
        "label_excess_return_5",
    ]
    execution_frame = panel.select(execution_columns).to_pandas()
    execution_frame["date"] = pd.to_datetime(execution_frame["date"])
    oos_frames: list[pd.DataFrame] = []
    for fold_index, (fold, (_, prediction)) in enumerate(zip(folds, fold_results, strict=True)):
        predicted = prediction.rename("prediction").reset_index()
        predicted = predicted.rename(columns={"datetime": "date", "instrument": "symbol"})
        predicted["fold"] = fold_index
        predicted["test_start"] = fold.test[0]
        predicted["test_end"] = fold.test[1]
        oos_frames.append(
            predicted.merge(
                execution_frame, on=["date", "symbol"], how="left", validate="one_to_one"
            )
        )
    oos_path = output_dir / "oos-predictions.parquet"
    pd.concat(oos_frames, ignore_index=True).to_parquet(oos_path, index=False)
    holdout_dataset = _dataset(tuning_frame, holdout_fold)
    holdout_model = _model(model_type, params)
    with R.start(experiment_name="ashare-frozen-post-cny-2026"):
        holdout_model.fit(holdout_dataset)
    holdout_prediction = holdout_model.predict(holdout_dataset, "test")
    holdout_frame = (
        holdout_prediction.rename("prediction")
        .reset_index()
        .rename(columns={"datetime": "date", "instrument": "symbol"})
    )
    holdout_frame["fold"] = -1
    holdout_frame["test_start"] = holdout_fold.test[0]
    holdout_frame["test_end"] = holdout_fold.test[1]
    holdout_frame = holdout_frame.merge(
        execution_frame,
        on=["date", "symbol"],
        how="left",
        validate="one_to_one",
    )
    holdout_path = output_dir / "holdout-post-cny-2026.parquet"
    holdout_frame.to_parquet(holdout_path, index=False)
    result = TrainingResult(
        model_version=model_version,
        model_type=model_type,
        feature_version=FEATURE_VERSION,
        data_cutoff=cutoff,
        artifact_path=str(artifact_path),
        oos_prediction_path=str(oos_path),
        holdout_prediction_path=str(holdout_path),
        oos_folds=len(folds),
        fold_rank_ic=fold_scores,
        median_rank_ic=float(np.nanmedian(fold_scores)),
        parameters=params,
        data_source="tushare-pro-point-in-time",
        promotable=True,
        training_windows=tuple(asdict(fold) for fold in folds),
    )
    (output_dir / "training-result.json").write_text(
        json.dumps(asdict(result), ensure_ascii=False, indent=2) + "\n", "utf-8"
    )
    return result
