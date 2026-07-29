from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd
from fastapi import HTTPException

_TEMP_DIR = tempfile.TemporaryDirectory()
os.environ["TUSHARE_TOKEN"] = "test-token"
os.environ["PYSERVER_DB_PATH"] = str(Path(_TEMP_DIR.name) / "test-cache.db")

import main  # noqa: E402


class FakeMinuteClient:
    def stk_mins(self, **kwargs):
        self.kwargs = kwargs
        return pd.DataFrame(
            [
                {
                    "ts_code": "000001.SZ",
                    "trade_time": "2026-07-23 09:31:00",
                    "open": 10.1,
                    "high": 10.2,
                    "low": 10.0,
                    "close": 10.15,
                    "vol": 200.0,
                    "amount": 2030.0,
                },
                {
                    "ts_code": "000001.SZ",
                    "trade_time": "2026-07-23 09:30:00",
                    "open": 10.0,
                    "high": 10.1,
                    "low": 9.9,
                    "close": 10.05,
                    "vol": 100.0,
                    "amount": 1005.0,
                },
            ]
        )


class MinuteKlineTests(unittest.TestCase):
    def setUp(self):
        with main.db() as conn:
            conn.execute("DELETE FROM cache")

    def test_date_only_range_uses_a_share_session_bounds(self):
        start, end = main._checked_minute_range("20260723", None)
        self.assertEqual(start.strftime("%Y-%m-%d %H:%M:%S"), "2026-07-23 09:30:00")
        self.assertEqual(end.strftime("%Y-%m-%d %H:%M:%S"), "2026-07-23 15:00:00")

    def test_range_is_bounded(self):
        with self.assertRaises(HTTPException) as raised:
            main._checked_minute_range("2026-01-01", "2026-02-02")
        self.assertEqual(raised.exception.status_code, 400)

    def test_minute_route_normalizes_and_sorts_rows(self):
        fake = FakeMinuteClient()
        with patch.object(main, "_pro", fake):
            result = main.minute_klines(
                symbol="000001",
                start="20260723",
                end=None,
                freq="1min",
            )
        self.assertFalse(result["realtime"])
        self.assertEqual(result["source"], "tushare_stk_mins")
        self.assertEqual(result["ts_code"], "000001.SZ")
        self.assertEqual(
            [bar["time"] for bar in result["bars"]],
            ["2026-07-23 09:30:00", "2026-07-23 09:31:00"],
        )
        self.assertEqual(fake.kwargs["start_date"], "2026-07-23 09:30:00")
        self.assertEqual(fake.kwargs["end_date"], "2026-07-23 15:00:00")

    def test_minute_route_rejects_hk_symbols(self):
        with self.assertRaises(HTTPException) as raised:
            main.minute_klines(
                symbol="hk00700",
                start="20260723",
                end=None,
                freq="1min",
            )
        self.assertEqual(raised.exception.status_code, 400)


class DailyHistoryEndpointTests(unittest.TestCase):
    def setUp(self):
        with main.db() as conn:
            conn.execute("DELETE FROM cache")

    def test_explicit_daily_range_is_preserved(self):
        start, end, explicit = main._checked_daily_range(
            20,
            "20260224",
            "20260724",
        )
        self.assertEqual((start, end, explicit), ("20260224", "20260724", True))

    def test_daily_range_rejects_reverse_dates(self):
        with self.assertRaises(HTTPException) as raised:
            main._checked_daily_range(20, "20260724", "20260224")
        self.assertEqual(raised.exception.status_code, 400)

    def test_moneyflow_explicit_range_does_not_tail_to_legacy_days(self):
        class FakeMoneyflowClient:
            def moneyflow(self, **kwargs):
                self.kwargs = kwargs
                return pd.DataFrame([
                    {
                        "trade_date": f"202601{i:02d}",
                        "buy_lg_amount": i,
                        "sell_lg_amount": 0,
                        "buy_elg_amount": 0,
                        "sell_elg_amount": 0,
                        "net_mf_amount": i,
                    }
                    for i in range(1, 6)
                ])

        fake = FakeMoneyflowClient()
        with patch.object(main, "_pro", fake), patch.object(main._MONEYFLOW_LIMITER, "acquire"):
            result = main.moneyflow(
                symbol="000001",
                days=2,
                start_date="20260101",
                end_date="20260131",
            )
        self.assertEqual(len(result.rows), 5)
        self.assertEqual(fake.kwargs["start_date"], "20260101")
        self.assertEqual(fake.kwargs["end_date"], "20260131")
        self.assertEqual([row.trade_date for row in result.rows], [
            "20260101", "20260102", "20260103", "20260104", "20260105",
        ])


if __name__ == "__main__":
    unittest.main()
