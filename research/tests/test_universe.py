import json
from datetime import date

import polars as pl

from ashare_research.universe import active_symbols_as_of, filter_frame_to_universe


def write_universe(path):
    path.write_text(
        json.dumps(
            {
                "entries": [
                    {"symbol": "300001", "pool_tier": "core"},
                    {
                        "symbol": "600001",
                        "pool_tier": "watch",
                        "strategy_from": "2026-01-01",
                        "strategy_until": "2026-06-30",
                    },
                    {"symbol": "688001", "pool_tier": "watch"},
                ]
            }
        ),
        "utf-8",
    )


def test_historical_watch_member_remains_active_inside_its_effective_range(tmp_path):
    universe = tmp_path / "universe.json"
    write_universe(universe)

    assert active_symbols_as_of(universe, "2026-06-01") == {"sz300001", "sh600001"}
    assert active_symbols_as_of(universe, "2026-07-01") == {"sz300001"}


def test_evaluation_frame_is_filtered_point_in_time(tmp_path):
    universe = tmp_path / "universe.json"
    write_universe(universe)
    frame = pl.DataFrame(
        {
            "date": [date(2026, 6, 1), date(2026, 7, 1), date(2026, 6, 1)],
            "symbol": ["sh600001", "sh600001", "sh688001"],
            "prediction": [1.0, 2.0, 3.0],
        }
    )

    filtered = filter_frame_to_universe(frame, universe)

    assert filtered.select("date", "symbol").rows() == [(date(2026, 6, 1), "sh600001")]
