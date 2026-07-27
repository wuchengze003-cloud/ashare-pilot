from datetime import date, timedelta

import polars as pl
import pytest

from ashare_research.features import build_feature_panel


def write_partition(root, endpoint, frame):
    target = root / "raw" / endpoint / "trade_date=20260101"
    target.mkdir(parents=True)
    frame.write_parquet(target / "part.parquet")


def test_labels_use_next_open_and_future_close(tmp_path):
    rows = []
    adjustments = []
    basics = []
    flows = []
    start = date(2026, 1, 1)
    for symbol in ("300001.SZ", "600001.SH"):
        for index in range(80):
            day = (start + timedelta(days=index)).strftime("%Y%m%d")
            base = 10 + index
            rows.append(
                {
                    "ts_code": symbol,
                    "trade_date": day,
                    "open": float(base),
                    "high": float(base + 1),
                    "low": float(base - 1),
                    "close": float(base + 0.5),
                    "vol": 1_000 + index,
                    "amount": 10_000 + index,
                }
            )
            adjustments.append({"ts_code": symbol, "trade_date": day, "adj_factor": 1.0})
            basics.append(
                {
                    "ts_code": symbol,
                    "trade_date": day,
                    "turnover_rate": 2.0,
                    "pe_ttm": 30.0,
                    "pb": 3.0,
                    "total_mv": 100_000.0,
                }
            )
            flows.append(
                {
                    "ts_code": symbol,
                    "trade_date": day,
                    "net_mf_amount": 10.0,
                    "buy_lg_amount": 50.0,
                    "sell_lg_amount": 40.0,
                }
            )
    write_partition(tmp_path, "daily", pl.DataFrame(rows))
    write_partition(tmp_path, "adj_factor", pl.DataFrame(adjustments))
    write_partition(tmp_path, "daily_basic", pl.DataFrame(basics))
    write_partition(tmp_path, "moneyflow", pl.DataFrame(flows))
    limit_rows = [
        {
            "ts_code": row["ts_code"],
            "trade_date": row["trade_date"],
            "up_limit": row["open"] * 1.1,
            "down_limit": row["open"] * 0.9,
        }
        for row in rows
    ]
    write_partition(tmp_path, "stk_limit", pl.DataFrame(limit_rows))
    reference = tmp_path / "reference"
    reference.mkdir()
    pl.DataFrame(
        {
            "ts_code": ["600001.SH"],
            "name": ["*ST测试"],
            "start_date": [(start + timedelta(days=65)).strftime("%Y%m%d")],
            "end_date": [(start + timedelta(days=69)).strftime("%Y%m%d")],
        }
    ).write_parquet(reference / "namechange.parquet")

    output = tmp_path / "features" / "panel.parquet"
    result = build_feature_panel(tmp_path, output, round_trip_fee_bps=10)
    panel = pl.read_parquet(output).filter(
        (pl.col("symbol") == "sz300001") & (pl.col("date") == date(2026, 3, 2))
    )
    assert result.rows == 37
    assert panel.height == 1
    expected = 73.5 / 71.0 - 1 - 0.001
    assert panel["label_return_3"][0] == pytest.approx(expected)
    assert panel["label_excess_return_3"][0] == pytest.approx(0)
    assert panel["label_downside_3"][0] == pytest.approx(70.0 / 71.0 - 1)
    assert panel["next_trade_date"][0] == date(2026, 3, 3)
    assert panel["next_open"][0] == 71.0
    assert panel["next_can_buy"][0]
    assert panel["next_can_sell"][0]
    all_rows = pl.read_parquet(output)
    assert all_rows.filter(
        (pl.col("symbol") == "sh600001")
        & (pl.col("date") == start + timedelta(days=65))
    ).is_empty()
    before_st = all_rows.filter(
        (pl.col("symbol") == "sh600001")
        & (pl.col("date") == start + timedelta(days=64))
    )
    assert before_st.height == 1
    assert not before_st["next_can_buy"][0]

    truncated = tmp_path / "features" / "truncated.parquet"
    truncated_result = build_feature_panel(
        tmp_path,
        truncated,
        round_trip_fee_bps=10,
        as_of_date="2026-03-05",
    )
    assert truncated_result.end_date == "2026-03-05"
    assert pl.read_parquet(truncated)["date"].max() == date(2026, 3, 5)


def test_nonpositive_pe_remains_missing_instead_of_creating_invalid_log(tmp_path):
    rows = []
    adjustments = []
    basics = []
    flows = []
    start = date(2025, 10, 1)
    for index in range(80):
        day = (start + timedelta(days=index)).strftime("%Y%m%d")
        rows.append(
            {
                "ts_code": "300001.SZ",
                "trade_date": day,
                "open": 10.0,
                "high": 10.5,
                "low": 9.5,
                "close": 10.2,
                "vol": 1_000.0,
                "amount": 10_000.0,
            }
        )
        adjustments.append(
            {"ts_code": "300001.SZ", "trade_date": day, "adj_factor": 1.0}
        )
        basics.append(
            {
                "ts_code": "300001.SZ",
                "trade_date": day,
                "turnover_rate": 2.0,
                "pe_ttm": -5.0 if index == 65 else 20.0,
                "pb": 3.0,
                "total_mv": 100_000.0,
            }
        )
        flows.append(
            {
                "ts_code": "300001.SZ",
                "trade_date": day,
                "net_mf_amount": 10.0,
                "buy_lg_amount": 50.0,
                "sell_lg_amount": 40.0,
            }
        )
    write_partition(tmp_path, "daily", pl.DataFrame(rows))
    write_partition(tmp_path, "adj_factor", pl.DataFrame(adjustments))
    write_partition(tmp_path, "daily_basic", pl.DataFrame(basics))
    write_partition(tmp_path, "moneyflow", pl.DataFrame(flows))
    write_partition(
        tmp_path,
        "stk_limit",
        pl.DataFrame(
            {
                "ts_code": ["300001.SZ"] * len(rows),
                "trade_date": [row["trade_date"] for row in rows],
                "up_limit": [11.0] * len(rows),
                "down_limit": [9.0] * len(rows),
            }
        ),
    )
    reference = tmp_path / "reference"
    reference.mkdir()
    pl.DataFrame(
        schema={
            "ts_code": pl.String,
            "name": pl.String,
            "start_date": pl.String,
            "end_date": pl.String,
        }
    ).write_parquet(reference / "namechange.parquet")

    output = tmp_path / "features" / "panel.parquet"
    build_feature_panel(tmp_path, output, round_trip_fee_bps=10)
    panel = pl.read_parquet(output).filter(
        pl.col("date") == start + timedelta(days=65)
    )

    assert panel.height == 1
    assert panel["log_pe_ttm"].null_count() == 1
