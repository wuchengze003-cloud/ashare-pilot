import json
from pathlib import Path

import pytest

from ashare_research.race_config import (
    load_race_config,
    race_contract_sha256,
)

CONFIG = Path(__file__).parent.parent / "config" / "production-race-v1.json"
CONFIG_V2 = Path(__file__).parent.parent / "config" / "production-race-v2.json"


def test_production_race_is_preregistered_for_three_candidates_and_one_champion():
    config = load_race_config(CONFIG)

    assert config.capital_yuan == 1_000_000
    assert config.minimum_calibrated_net_return == 0
    assert config.ranking_switch_buffer == 0.05
    assert config.champion_slots == 1
    assert len(config.candidates) == 3
    assert len({candidate.family for candidate in config.candidates}) == 3
    assert config.promotion_gates.maximum_drawdown_pct == 15
    assert config.promotion_gates.minimum_oos_sharpe == 1.5
    assert config.promotion_gates.minimum_frozen_sharpe == 1.25
    assert config.promotion_gates.minimum_oos_annualized_return_pct == 15
    assert config.promotion_gates.maximum_downside_capture == 0.75
    assert config.windows["frozen"].end is None
    assert all(candidate.parameter_sets() for candidate in config.candidates)
    assert all(
        candidate.minute_parameter_sets() for candidate in config.candidates
    )
    assert len(race_contract_sha256(config)) == 64


def test_race_rejects_using_frozen_window_for_parameter_selection(tmp_path):
    raw = json.loads(CONFIG.read_text("utf-8"))
    raw["windows"]["oos"]["end"] = "2026-03-01"
    target = tmp_path / "race.json"
    target.write_text(json.dumps(raw), "utf-8")

    with pytest.raises(ValueError, match="overlap"):
        load_race_config(target)


def test_race_rejects_more_than_one_production_champion(tmp_path):
    raw = json.loads(CONFIG.read_text("utf-8"))
    raw["champion_slots"] = 2
    target = tmp_path / "race.json"
    target.write_text(json.dumps(raw), "utf-8")

    with pytest.raises(ValueError, match="exactly one"):
        load_race_config(target)


def test_race_rejects_duplicate_alpha_families(tmp_path):
    raw = json.loads(CONFIG.read_text("utf-8"))
    raw["candidates"][1]["family"] = raw["candidates"][0]["family"]
    target = tmp_path / "race.json"
    target.write_text(json.dumps(raw), "utf-8")

    with pytest.raises(ValueError, match="families must be unique"):
        load_race_config(target)


def test_v2_race_allows_daily_candidates_without_minute_grids():
    config = load_race_config(CONFIG_V2)

    assert config.schema == "ashare-production-race/v2"
    assert len(config.candidates) == 3
    assert all(candidate.signal_frequency == "1d" for candidate in config.candidates)
    assert all(not candidate.requires_minute for candidate in config.candidates)
    assert all(candidate.minute_parameter_sets() == () for candidate in config.candidates)
    assert all(
        candidate.calibration_label == "label_excess_return_10"
        for candidate in config.candidates
    )
    assert all(candidate.label_horizon_bars == 10 for candidate in config.candidates)


def test_v2_hybrid_candidate_requires_declared_minute_data(tmp_path):
    raw = json.loads(CONFIG_V2.read_text("utf-8"))
    raw["candidates"][0]["signal_frequency"] = "1d+5min"
    target = tmp_path / "race.json"
    target.write_text(json.dumps(raw), "utf-8")

    with pytest.raises(ValueError, match="requires 5min data"):
        load_race_config(target)
