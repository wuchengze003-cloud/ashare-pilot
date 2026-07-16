from datetime import date, timedelta

import polars as pl

from ashare_research.evaluation import evaluate_oos_predictions
from ashare_research.portfolio import PortfolioConfig


def test_evaluation_generates_signal_and_next_open_portfolio_metrics(tmp_path):
    rows = []
    start = date(2026, 1, 1)
    for day_index in range(20):
        day = start + timedelta(days=day_index)
        for symbol_index, symbol in enumerate(("sz300001", "sz300002", "sh600001")):
            prediction = 0.03 - symbol_index * 0.01
            realized = 0.02 - symbol_index * 0.01
            rows.append(
                {
                    "date": day,
                    "symbol": symbol,
                    "prediction": prediction,
                    "label_return_5": realized,
                    "adj_close": 10.0,
                    "next_trade_date": day + timedelta(days=1),
                    "next_open": 10.0,
                    "next_close": 10.0 * (1 + realized / 5),
                    "next_can_buy": True,
                    "next_can_sell": True,
                    "fold": day_index // 5,
                }
            )
    path = tmp_path / "oos.parquet"
    pl.DataFrame(rows).write_parquet(path)
    report = evaluate_oos_predictions(
        path,
        PortfolioConfig(max_positions=1, fee_bps=0, rebalance_threshold_pct=0),
    )
    assert report.signal.rank_ic > 0.99
    assert report.signal.precision_at_k == 1
    assert report.signal.ndcg_at_k > 0.99
    assert report.oos_folds == 4
    assert report.portfolio.total_return_pct > 0
    assert report.source_sha256


def test_randomized_labels_remove_rank_advantage(tmp_path):
    rows = []
    start = date(2026, 1, 1)
    labels = (0.02, -0.02, 0.0)
    for day_index in range(12):
        day = start + timedelta(days=day_index)
        rotated = labels[day_index % 3 :] + labels[: day_index % 3]
        for symbol_index, symbol in enumerate(("sz300001", "sz300002", "sh600001")):
            rows.append(
                {
                    "date": day,
                    "symbol": symbol,
                    "prediction": 0.03 - symbol_index * 0.01,
                    "label_return_5": rotated[symbol_index],
                    "adj_close": 10.0,
                    "next_trade_date": day + timedelta(days=1),
                    "next_open": 10.0,
                    "next_close": 10.0,
                    "next_can_buy": True,
                    "next_can_sell": True,
                    "fold": 0,
                }
            )
    path = tmp_path / "random.parquet"
    pl.DataFrame(rows).write_parquet(path)
    report = evaluate_oos_predictions(path)
    assert abs(report.signal.rank_ic) < 0.01
