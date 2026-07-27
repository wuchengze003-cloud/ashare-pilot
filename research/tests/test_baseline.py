import json

from ashare_research.baseline import export_v1_baseline


def test_v1_export_preserves_curve_and_computes_holding_time(tmp_path):
    backtest = {
        "latestDate": "2026-07-03",
        "stats": {
            "totalReturnPct": 10,
            "sharpe": 2,
            "maxDrawdownPct": -5,
            "turnoverPct": 100,
        },
        "equityCurve": [
            {"date": "2026-07-01", "equity": 100, "cash": 0, "positions": {}},
            {"date": "2026-07-02", "equity": 105, "cash": 0, "positions": {}},
            {"date": "2026-07-03", "equity": 110, "cash": 110, "positions": {}},
        ],
        "trades": [
            {"date": "2026-07-01", "symbol": "300001", "side": "buy", "shares": 100},
            {"date": "2026-07-03", "symbol": "300001", "side": "sell", "shares": 100},
        ],
    }
    source = tmp_path / "backtest.json"
    source.write_text(json.dumps(backtest), "utf-8")
    report, metrics = export_v1_baseline(source, tmp_path / "out")
    assert report["portfolio"]["equity_curve"][-1]["equity"] == 110
    assert metrics.average_hold_bars == 3
    assert metrics.closed_trades == 1
