from datetime import date

import polars as pl
import pytest

from ashare_research.shadow import advance_shadow_account, set_pending_targets
from ashare_research.shadow_evaluation import evaluate_shadow_account


def panel(day, close):
    return pl.DataFrame(
        [
            {
                "date": day,
                "symbol": "sz300001",
                "open": 10.0,
                "close": close,
                "adj_factor": 1.0,
                "can_buy_open": True,
                "can_sell_open": True,
            }
        ]
    )


def test_shadow_evaluation_uses_realized_equity_and_executions(tmp_path):
    state_path = tmp_path / "state.json"
    ledger = tmp_path / "ledger.db"
    state = advance_shadow_account(state_path, panel(date(2026, 7, 1), 10), ledger, "v2")
    set_pending_targets(state_path, state, "2026-07-01", {"300001": 1.0})
    advance_shadow_account(state_path, panel(date(2026, 7, 2), 11), ledger, "v2", fee_bps=0)
    report = evaluate_shadow_account(state_path, ledger)
    assert report["portfolio"]["total_return_pct"] == pytest.approx(10)
    assert report["portfolio"]["trades"] == 1
    assert report["portfolio"]["average_hold_bars"] >= 1


def test_closed_holding_period_uses_all_shadow_trading_days(tmp_path):
    state_path = tmp_path / "state.json"
    ledger = tmp_path / "ledger.db"
    state = advance_shadow_account(state_path, panel(date(2026, 7, 1), 10), ledger, "v2")
    set_pending_targets(state_path, state, "2026-07-01", {"300001": 1.0})
    advance_shadow_account(state_path, panel(date(2026, 7, 2), 10), ledger, "v2", fee_bps=0)
    held = advance_shadow_account(
        state_path,
        panel(date(2026, 7, 3), 10),
        ledger,
        "v2",
        fee_bps=0,
    )
    set_pending_targets(state_path, held, "2026-07-03", {})
    advance_shadow_account(state_path, panel(date(2026, 7, 4), 10), ledger, "v2", fee_bps=0)

    report = evaluate_shadow_account(state_path, ledger)

    assert report["portfolio"]["closed_trades"] == 1
    assert report["portfolio"]["average_hold_bars"] == 3
