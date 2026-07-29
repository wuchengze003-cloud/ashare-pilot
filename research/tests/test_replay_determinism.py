"""Replay-determinism guard for the feature pipeline (merge blocker 1).

Building a feature panel with ``as_of_date=T1`` must produce identical output
regardless of whether the data source also contains data after T1 — including
a corporate action (adj_factor jump) that happens after T1.

If a future refactor switches adjusted prices to a latest-factor-normalized
scheme (前复权/qfq) or weakens the as_of filter, the "late" run would rewrite
history and this test fails.
"""

from datetime import date, timedelta

import polars as pl

from ashare_research.contracts import HORIZONS
from ashare_research.features import build_feature_panel

START = date(2024, 1, 2)
SYMBOLS = ("000001.SZ", "000002.SZ", "600000.SH")
TOTAL_DAYS = 120
T1_INDEX = 99  # as_of date = START + 99 days
# Corporate action strictly after T1 + max(HORIZONS) so truncated labels are complete.
SPLIT_INDEX = T1_INDEX + max(HORIZONS) + 2
TRUNCATED_DAYS = T1_INDEX + max(HORIZONS) + 1  # data source "yesterday"
NUMERIC_TOLERANCE = 1e-9


def _write_partition(root, endpoint, frame):
    target = root / "raw" / endpoint / "trade_date=20240102"
    target.mkdir(parents=True, exist_ok=True)
    frame.write_parquet(target / "part.parquet")


def _make_rows(num_days):
    daily, adjustments, limits = [], [], []
    for symbol_index, symbol in enumerate(SYMBOLS):
        for index in range(num_days):
            day = (START + timedelta(days=index)).strftime("%Y%m%d")
            # adj_factor history for dates <= truncation is byte-identical in
            # both sources: Tushare historical factors never change when a
            # later corporate action arrives.
            factor = 1.0 if index < SPLIT_INDEX else 2.0
            scale = 1.0 if index < SPLIT_INDEX else 0.5
            base = float(10 + symbol_index * 5 + index)
            open_ = base * scale
            daily.append(
                {
                    "ts_code": symbol,
                    "trade_date": day,
                    "open": open_,
                    "high": (base + 1.0) * scale,
                    "low": (base - 1.0) * scale,
                    "close": (base + 0.5) * scale,
                    "vol": float(1_000 + index),
                    "amount": float(10_000 + index),
                }
            )
            adjustments.append(
                {"ts_code": symbol, "trade_date": day, "adj_factor": factor}
            )
            limits.append(
                {
                    "ts_code": symbol,
                    "trade_date": day,
                    "up_limit": open_ * 1.1,
                    "down_limit": open_ * 0.9,
                }
            )
    return daily, adjustments, limits


def _build_panel(root, num_days, as_of):
    daily, adjustments, limits = _make_rows(num_days)
    _write_partition(root, "daily", pl.DataFrame(daily))
    _write_partition(root, "adj_factor", pl.DataFrame(adjustments))
    _write_partition(root, "stk_limit", pl.DataFrame(limits))
    output = root / "features" / "panel.parquet"
    build_feature_panel(root, output, round_trip_fee_bps=10, as_of_date=as_of)
    return pl.read_parquet(output).sort("symbol", "date")


def test_feature_panel_replay_deterministic_under_future_data(tmp_path):
    as_of = str(START + timedelta(days=T1_INDEX))
    early = _build_panel(tmp_path / "early", TRUNCATED_DAYS, as_of)
    late = _build_panel(tmp_path / "late", TOTAL_DAYS, as_of)

    # Both runs see identical data <= T1; only the late run also has rows in
    # (T1, T2], including the adj_factor jump at SPLIT_INDEX.
    assert early.height > 0
    assert early.columns == late.columns
    assert early.height == late.height

    numeric_cols = [
        name
        for name, dtype in zip(early.columns, early.dtypes)
        if dtype in (pl.Float64, pl.Float32, pl.Int64, pl.Int32, pl.UInt32, pl.UInt64)
    ]
    discrete_cols = [name for name in early.columns if name not in numeric_cols]

    diverged = []
    for name in discrete_cols:
        if early[name].to_list() != late[name].to_list():
            diverged.append(name)
    assert not diverged, f"discrete columns diverged under replay: {diverged}"

    numeric_diverged = []
    for name in numeric_cols:
        for x, y in zip(early[name].to_list(), late[name].to_list()):
            if x is None or y is None:
                if (x is None) != (y is None):
                    numeric_diverged.append((name, x, y))
                    break
            elif abs(x - y) > NUMERIC_TOLERANCE:
                numeric_diverged.append((name, x, y))
                break
    assert not numeric_diverged, (
        f"numeric columns diverged beyond {NUMERIC_TOLERANCE}: {numeric_diverged[:3]}"
    )


def test_feature_panel_labels_unchanged_when_future_corporate_action_arrives(tmp_path):
    """Labels at dates whose full horizon fits inside the truncated source must
    be identical after a later corporate action enters the data source."""
    as_of = str(START + timedelta(days=T1_INDEX))
    early = _build_panel(tmp_path / "early", TRUNCATED_DAYS, as_of)
    late = _build_panel(tmp_path / "late", TOTAL_DAYS, as_of)

    label_cols = [c for c in early.columns if c.startswith("label_")]
    assert label_cols, "expected label columns in feature panel"
    for name in label_cols:
        for x, y in zip(early[name].to_list(), late[name].to_list()):
            if x is None or y is None:
                assert (x is None) == (y is None), f"{name}: null mismatch"
            else:
                assert abs(x - y) <= NUMERIC_TOLERANCE, f"{name}: {x} vs {y}"
