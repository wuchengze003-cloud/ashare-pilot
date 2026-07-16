from datetime import date

import polars as pl

from ashare_research.ledger import ledger_counts
from ashare_research.shadow import advance_shadow_account, set_pending_targets


def panel(
    day,
    *,
    can_buy=True,
    can_sell=True,
    open_price=10.0,
    close_price=10.0,
    adj_factor=1.0,
):
    return pl.DataFrame(
        [
            {
                "date": day,
                "symbol": "sz300001",
                "open": open_price,
                "close": close_price,
                "adj_factor": adj_factor,
                "can_buy_open": can_buy,
                "can_sell_open": can_sell,
            }
        ]
    )


def test_shadow_executes_previous_decision_at_next_open_and_is_idempotent(tmp_path):
    state_path = tmp_path / "state.json"
    ledger = tmp_path / "ledger.db"
    state = advance_shadow_account(
        state_path, panel(date(2026, 7, 1)), ledger, "model-v2", fee_bps=0
    )
    set_pending_targets(state_path, state, "2026-07-01", {"300001": 1.0})
    executed = advance_shadow_account(
        state_path,
        panel(date(2026, 7, 2), open_price=10, close_price=11),
        ledger,
        "model-v2",
        fee_bps=0,
    )
    assert executed["positions"] == {"300001": 100_000}
    assert executed["equity_curve"][-1]["equity"] == 1_100_000
    assert ledger_counts(ledger)["executions"] == 1
    repeated = advance_shadow_account(
        state_path,
        panel(date(2026, 7, 2), open_price=10, close_price=11),
        ledger,
        "model-v2",
        fee_bps=0,
    )
    assert len(repeated["equity_curve"]) == 2
    assert ledger_counts(ledger)["executions"] == 1


def test_shadow_records_rejected_limit_up_buy(tmp_path):
    state_path = tmp_path / "state.json"
    ledger = tmp_path / "ledger.db"
    state = advance_shadow_account(state_path, panel(date(2026, 7, 1)), ledger, "model-v2")
    set_pending_targets(state_path, state, "2026-07-01", {"300001": 1.0})
    executed = advance_shadow_account(
        state_path,
        panel(date(2026, 7, 2), can_buy=False),
        ledger,
        "model-v2",
    )
    assert executed["positions"] == {}
    assert ledger_counts(ledger)["executions"] == 1


def test_shadow_uses_raw_prices_and_adjusts_shares_on_corporate_action(tmp_path):
    state_path = tmp_path / "state.json"
    ledger = tmp_path / "ledger.db"
    state = advance_shadow_account(state_path, panel(date(2026, 7, 1)), ledger, "model-v2")
    set_pending_targets(state_path, state, "2026-07-01", {"300001": 1.0})
    invested = advance_shadow_account(
        state_path,
        panel(date(2026, 7, 2), open_price=10, close_price=10, adj_factor=1),
        ledger,
        "model-v2",
        fee_bps=0,
    )
    set_pending_targets(state_path, invested, "2026-07-02", {"300001": 1.0})

    adjusted = advance_shadow_account(
        state_path,
        panel(date(2026, 7, 3), open_price=5, close_price=5, adj_factor=2),
        ledger,
        "model-v2",
        fee_bps=0,
    )

    assert adjusted["positions"] == {"300001": 200_000}
    assert adjusted["equity_curve"][-1]["equity"] == 1_000_000
    assert ledger_counts(ledger)["executions"] == 1
