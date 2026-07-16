from datetime import date

import pytest

from ashare_research.contracts import DecisionEvent, PredictionRecord, PriceBar
from ashare_research.ledger import (
    append_decision,
    append_prediction,
    backfill_outcomes,
    init_ledger,
    ledger_counts,
    read_outcomes,
    read_predictions,
    summarize_outcomes,
)


def prediction(**overrides):
    values = {
        "decision_date": date(2026, 7, 1),
        "data_cutoff": date(2026, 7, 1),
        "symbol": "sz300001",
        "model_version": "lgbm-001",
        "feature_version": "alpha-core-v1",
        "horizon_bars": 3,
        "expected_return": 0.04,
        "downside_return": -0.02,
        "confidence": 0.8,
        "rank": 1,
        "target_weight": 0.25,
        "action": "buy",
        "reason_codes": ("POSITIVE_UTILITY",),
    }
    values.update(overrides)
    return PredictionRecord(**values)


def test_prediction_rejects_future_data():
    with pytest.raises(ValueError, match="data_cutoff"):
        prediction(data_cutoff=date(2026, 7, 2))


def test_prediction_ledger_is_idempotent_and_point_in_time(tmp_path):
    db = tmp_path / "ledger.db"
    init_ledger(db)
    record = prediction()
    assert append_prediction(db, record)
    assert not append_prediction(db, record)
    assert ledger_counts(db)["predictions"] == 1
    assert read_predictions(db, as_of=date(2026, 6, 30)) == []
    assert read_predictions(db, as_of=date(2026, 7, 1)) == [record]


def test_decision_ledger_is_append_only_and_idempotent(tmp_path):
    db = tmp_path / "ledger.db"
    init_ledger(db)
    event = DecisionEvent(
        decision_date=date(2026, 7, 1),
        model_version="lgbm-001",
        symbol="300346",
        action="buy",
        rank=1,
        target_weight_before=0,
        target_weight_after=0.25,
        expected_return=0.03,
        downside_risk=-0.02,
        reason_codes=("POSITIVE_NET_UTILITY",),
    )
    assert append_decision(db, event)
    assert not append_decision(db, event)
    assert ledger_counts(db)["decisions"] == 1


def test_outcome_uses_next_open_and_waits_for_evaluation_date(tmp_path):
    db = tmp_path / "ledger.db"
    init_ledger(db)
    append_prediction(db, prediction())
    bars = [
        PriceBar(date(2026, 7, 1), "sz300001", 9, 11, 8, 10),
        PriceBar(date(2026, 7, 2), "sz300001", 10, 12, 9, 11),
        PriceBar(date(2026, 7, 3), "sz300001", 11, 13, 10, 12),
        PriceBar(date(2026, 7, 6), "sz300001", 12, 14, 8, 13),
        PriceBar(date(2026, 7, 7), "sz300001", 30, 31, 29, 30),
    ]
    assert backfill_outcomes(db, bars, as_of_date=date(2026, 7, 3)) == 0
    assert backfill_outcomes(db, bars, as_of_date=date(2026, 7, 6), round_trip_fee_bps=10) == 1
    outcome = read_outcomes(db)[0]
    assert outcome.entry_date == date(2026, 7, 2)
    assert outcome.entry_open == 10
    assert outcome.evaluation_date == date(2026, 7, 6)
    assert outcome.exit_close == 13
    assert outcome.net_return == pytest.approx(0.299)
    assert outcome.mfe == pytest.approx(0.4)
    assert outcome.mae == pytest.approx(-0.2)


def test_outcome_tracks_opportunity_cost(tmp_path):
    db = tmp_path / "ledger.db"
    init_ledger(db)
    append_prediction(db, prediction(symbol="sz300001", rank=1))
    append_prediction(db, prediction(symbol="sz300002", rank=2))
    bars = [
        PriceBar(date(2026, 7, 2), "sz300001", 10, 11, 9, 10),
        PriceBar(date(2026, 7, 3), "sz300001", 10, 11, 9, 10),
        PriceBar(date(2026, 7, 6), "sz300001", 10, 11, 9, 11),
        PriceBar(date(2026, 7, 2), "sz300002", 10, 11, 9, 10),
        PriceBar(date(2026, 7, 3), "sz300002", 10, 12, 9, 11),
        PriceBar(date(2026, 7, 6), "sz300002", 11, 13, 10, 13),
    ]
    assert backfill_outcomes(db, bars, as_of_date=date(2026, 7, 6), round_trip_fee_bps=0) == 2
    outcomes = {outcome.symbol: outcome for outcome in read_outcomes(db)}
    assert outcomes["sz300002"].opportunity_cost == 0
    assert outcomes["sz300001"].opportunity_cost == pytest.approx(0.2)
    summary = summarize_outcomes(db)
    group = summary["groups"][0]
    assert group["observations"] == 2
    assert group["hit_rate"] == 0.5
    assert group["net_hit_rate"] == 1
    assert group["mean_opportunity_cost"] == pytest.approx(0.1)
    assert group["mean_excess_return"] == pytest.approx(0)
