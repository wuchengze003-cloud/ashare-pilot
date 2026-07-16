from datetime import date, timedelta

import pandas as pd
import polars as pl
from qlib.data.dataset.handler import DataHandlerLP

from ashare_research.training import (
    WalkForwardFold,
    _dataset,
    _optimize_lightgbm,
    _qlib_frame,
    make_frozen_holdout_fold,
    make_walk_forward_folds,
)


def test_walk_forward_has_six_purged_folds_before_holdout():
    start = date(2017, 1, 1)
    dates = [start + timedelta(days=index) for index in range(2_400)]
    folds = make_walk_forward_folds(dates, holdout_start=date(2026, 2, 24))
    assert len(folds) >= 6
    positions = {value.isoformat(): index for index, value in enumerate(dates)}
    for fold in folds:
        assert positions[fold.valid[0]] - positions[fold.train[1]] == 11
        assert positions[fold.test[0]] - positions[fold.valid[1]] == 11
        assert fold.test[1] < "2026-02-24"


def test_frozen_holdout_never_trains_on_post_cny_2026():
    start = date(2018, 1, 1)
    dates = [start + timedelta(days=index) for index in range(3_100)]
    fold = make_frozen_holdout_fold(dates)
    positions = {value.isoformat(): index for index, value in enumerate(dates)}
    assert fold.train[1] < "2026-02-24"
    assert fold.valid[1] < "2026-02-24"
    assert fold.test[0] >= "2026-02-24"
    assert positions[fold.test[0]] - positions[fold.valid[1]] == 11


def test_production_dataset_keeps_labels_in_return_units():
    rows = []
    start = date(2025, 1, 1)
    for index in range(6):
        for symbol, label in (("A", 0.01), ("B", 0.03)):
            rows.append(
                {
                    "date": start + timedelta(days=index),
                    "symbol": symbol,
                    "feature": float(index),
                    "label": label,
                }
            )
    frame = _qlib_frame(pl.DataFrame(rows), ["feature"], "label")
    fold = WalkForwardFold(
        train=("2025-01-01", "2025-01-03"),
        valid=("2025-01-04", "2025-01-04"),
        test=("2025-01-05", "2025-01-06"),
    )
    dataset = _dataset(frame, fold)
    labels = dataset.prepare("train", col_set="label", data_key=DataHandlerLP.DK_L)

    assert set(labels.iloc[:, 0].round(6)) == {0.01, 0.03}


def test_optuna_never_scores_the_oos_test_segment(monkeypatch):
    fold = WalkForwardFold(
        train=("2020-01-01", "2024-12-31"),
        valid=("2025-01-01", "2025-06-30"),
        test=("2025-07-01", "2025-09-30"),
    )
    segments = []

    def fake_evaluate(frame, received_fold, model_type, params, segment):
        segments.append(segment)
        return 0.02, pd.Series(dtype=float)

    monkeypatch.setattr("ashare_research.training._evaluate_segment", fake_evaluate)
    _optimize_lightgbm(pd.DataFrame(), [fold, fold, fold], trials=1)

    assert segments
    assert set(segments) == {"valid"}
