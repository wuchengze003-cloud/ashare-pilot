"""Official Qlib Alpha158 benchmark, isolated from production promotion."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from .qlib_config import sqlite_exp_manager
from .training import make_frozen_holdout_fold, make_walk_forward_folds


@dataclass(frozen=True)
class Alpha158BenchmarkResult:
    source: str
    promotable: bool
    market: str
    model_type: str
    label: str
    feature_count: int
    data_start: str
    data_end: str
    fold_rank_ic: tuple[float, ...]
    median_rank_ic: float
    holdout_rank_ic: float
    oos_prediction_path: str
    holdout_prediction_path: str


def alpha158_next_open_label(horizon: int = 5, fee_bps: float = 10) -> str:
    if horizon < 1:
        raise ValueError("horizon must be positive")
    return f"Ref($close,-{horizon})/Ref($open,-1)-1-{fee_bps / 10_000:.8f}"


def _rank_ic(prediction: pd.Series, label: pd.Series) -> float:
    joined = pd.concat({"prediction": prediction, "label": label}, axis=1).dropna()
    if joined.empty:
        return 0.0
    values = joined.groupby(level="datetime").apply(
        lambda group: group["prediction"].corr(group["label"], method="spearman"),
        include_groups=False,
    )
    return float(values.replace([np.inf, -np.inf], np.nan).dropna().mean())


def _model(model_type: str):
    if model_type == "linear":
        from qlib.contrib.model.linear import LinearModel

        return LinearModel(estimator="ridge", alpha=1.0, fit_intercept=True)
    if model_type == "lightgbm":
        from qlib.contrib.model.gbdt import LGBModel

        return LGBModel(
            loss="mse",
            learning_rate=0.03,
            num_leaves=31,
            max_depth=8,
            subsample=0.8,
            colsample_bytree=0.8,
            lambda_l1=1.0,
            lambda_l2=5.0,
            num_boost_round=500,
            early_stopping_rounds=50,
            num_threads=8,
        )
    raise ValueError(f"unsupported benchmark model: {model_type}")


def _evaluate(model_type: str, handler, fold) -> tuple[float, pd.DataFrame]:
    from qlib.data.dataset import DatasetH
    from qlib.data.dataset.handler import DataHandlerLP

    dataset = DatasetH(handler=handler, segments=asdict(fold))
    model = _model(model_type)
    model.fit(dataset)
    prediction = model.predict(dataset, "test")
    label = dataset.prepare("test", col_set="label", data_key=DataHandlerLP.DK_L).iloc[:, 0]
    frame = prediction.rename("prediction").to_frame().join(label.rename("label"))
    return _rank_ic(prediction, label), frame.reset_index()


def run_alpha158_benchmark(
    runtime_root: Path | str,
    model_type: str = "linear",
    market: str = "csi500",
    max_folds: int | None = None,
) -> Alpha158BenchmarkResult:
    import qlib
    from qlib.config import REG_CN
    from qlib.contrib.data.handler import Alpha158
    from qlib.contrib.data.loader import Alpha158DL
    from qlib.data import D

    runtime_root = Path(runtime_root)
    provider = runtime_root / "qlib" / "cn_data"
    qlib.init(
        provider_uri=str(provider.resolve()),
        region=REG_CN,
        kernels=4,
        exp_manager=sqlite_exp_manager(runtime_root / "mlflow.db"),
    )
    calendar = [value.date() for value in D.calendar(start_time="2018-01-01", freq="day")]
    folds = make_walk_forward_folds(calendar)
    if max_folds is not None:
        folds = folds[:max_folds]
    holdout = make_frozen_holdout_fold(calendar)
    label = alpha158_next_open_label()
    output_dir = runtime_root / "benchmarks" / f"alpha158-{model_type}-{market}"
    output_dir.mkdir(parents=True, exist_ok=True)
    handler_cache = (
        runtime_root
        / "cache"
        / (f"alpha158-{market}-{calendar[0].isoformat()}-{calendar[-1].isoformat()}.pkl")
    )
    if handler_cache.exists():
        handler = Alpha158.load(handler_cache)
    else:
        handler = Alpha158(
            instruments=market,
            start_time=calendar[0].isoformat(),
            end_time=calendar[-1].isoformat(),
            fit_start_time=calendar[0].isoformat(),
            fit_end_time=folds[0].train[1],
            infer_processors=[{"class": "ProcessInf"}, {"class": "Fillna"}],
            learn_processors=[
                {"class": "DropnaLabel"},
                {"class": "CSZScoreNorm", "kwargs": {"fields_group": "label"}},
            ],
            label=([label], ["LABEL0"]),
        )
        handler_cache.parent.mkdir(parents=True, exist_ok=True)
        handler.to_pickle(handler_cache, dump_all=True)
    scores: list[float] = []
    predictions: list[pd.DataFrame] = []
    for index, fold in enumerate(folds):
        score, frame = _evaluate(model_type, handler, fold)
        scores.append(score)
        frame["fold"] = index
        predictions.append(frame)
    oos_path = output_dir / "oos-predictions.parquet"
    pd.concat(predictions, ignore_index=True).to_parquet(oos_path, index=False)
    holdout_score, holdout_frame = _evaluate(model_type, handler, holdout)
    holdout_path = output_dir / "holdout-post-cny-2026.parquet"
    holdout_frame.to_parquet(holdout_path, index=False)
    _, names = Alpha158DL.get_feature_config()
    result = Alpha158BenchmarkResult(
        source="community-qlib-cold-start",
        promotable=False,
        market=market,
        model_type=model_type,
        label=label,
        feature_count=len(names),
        data_start=calendar[0].isoformat(),
        data_end=calendar[-1].isoformat(),
        fold_rank_ic=tuple(scores),
        median_rank_ic=float(np.median(scores)),
        holdout_rank_ic=holdout_score,
        oos_prediction_path=str(oos_path),
        holdout_prediction_path=str(holdout_path),
    )
    (output_dir / "result.json").write_text(
        json.dumps(asdict(result), ensure_ascii=False, indent=2) + "\n", "utf-8"
    )
    return result
