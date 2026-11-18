"""A failing diagnostic shadow cannot block normal Paper evaluation."""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from tests.test_strategy import scenario
from weather99.config import load_config
from weather99.domain import MODELS, timestamp
from weather99.paper import DEFAULT_PAPER_CONFIG, process_capture, read_settings
from weather99.paper_ledger import PaperLedger
from weather99.settlement import metar_evidence


class ShadowIsolationTests(unittest.TestCase):
    def test_only_verified_settlement_supplies_shadow_high(self):
        target = "2026-11-18"
        as_of = f"{target}T10:45:00+08:00"
        instant = timestamp(as_of).timestamp()
        observations = [
            {"icaoId": "ZSPD", "obsTime": instant - 900, "temp": 25,
             "rawOb": "METAR ZSPD 180230Z 00000KT 25/14"},
            {"icaoId": "ZSPD", "obsTime": instant - 300, "temp": 35,
             "rawOb": "SPECI ZSPD 180240Z 00000KT 35/14"},
        ]
        raw = {
            "schema": "weather99.snapshot.v1",
            "station": "ZSPD",
            "timezone": "Asia/Shanghai",
            "collected_at": as_of,
            "event_slug": "shanghai-test",
            "forecast": {"date": target, "models_c": dict.fromkeys(MODELS, 25)},
            "buckets": [],
            "sources": {
                "event": {"fetched_at": as_of, "data": {
                    "description": "ZSPD resolution", "slug": "shanghai-test",
                    "active": True, "endDate": f"{target}T23:59:00+08:00",
                }},
                "forecast": {"fetched_at": as_of, "data": {"hourly": {
                    "time": [f"{target}T{hour:02d}:00" for hour in range(24)],
                    **{f"temperature_2m_{model}": [25] * 24 for model in MODELS},
                }}},
                "observation": {"data": observations},
            },
        }
        settlement = {
            "rules_verified": True,
            "station": "ZSPD",
            "target_date": target,
            **metar_evidence(observations, target, as_of),
        }
        settings = read_settings(DEFAULT_PAPER_CONFIG)
        strategy, strategy_raw = load_config()
        with TemporaryDirectory() as folder:
            path = Path(folder)
            with PaperLedger(path / "paper.sqlite3", settings["initial_cash_usd"]) as ledger:
                for capture, expected_status, expected_high in (
                    (raw, "verified_station_high_missing", None),
                    (raw | {"settlement": settlement}, "shadow_ready", 25),
                ):
                    with self.subTest(expected_status=expected_status):
                        result = process_capture(
                            capture, path / "capture.json", settings, strategy,
                            strategy_raw, ledger, path / "audit.sqlite3",
                        )
                        self.assertIn("signal_id", result)
                        shadow = result["hourly_shadow"]
                        self.assertEqual(shadow["status"], expected_status)
                        self.assertEqual(shadow.get("predicted_final_high_c"), expected_high)
                        if expected_high is None:
                            self.assertEqual(result["forecast"]["center_c"], 35)

    def test_shadow_exception_does_not_block_legacy_paper(self):
        snapshot, _ = scenario()
        raw = {
            "collected_at": snapshot.as_of,
            "forecast": {
                "date": snapshot.target_date,
                "models_c": dict(zip(MODELS, (17, 18, 19, 20))),
            },
            "sources": {
                "event": {"data": {"description": "ZSPD resolution"}},
                "forecast": {"fetched_at": snapshot.as_of},
            },
            "errors": {},
        }
        ledger = Mock()
        ledger.settle.return_value = []
        ledger.account.return_value = {"valuation_complete": True}
        ledger.buy.return_value = {"status": "no_fill", "reason": "signal_not_confirmed"}
        signal = {
            "status": "rejected",
            "id": "test-signal",
            "reasons": ["no_eligible_combo"],
            "confirmation_count": 0,
            "selected": None,
            "candidates": [],
        }
        strategy, raw_strategy = load_config()
        with (
            TemporaryDirectory() as folder,
            patch("weather99.paper.live_snapshot", return_value=snapshot),
            patch("weather99.paper.correct_forecast",
                  side_effect=lambda raw, forecast, floor: (forecast, {"status": "not_target_day"})),
            patch("weather99.paper.hourly_tmax_shadow", side_effect=RuntimeError("shadow defect")),
            patch("weather99.paper.analyze", return_value=signal),
        ):
            path = Path(folder)
            with self.assertLogs("weather99.paper", level="ERROR"):
                result = process_capture(
                    raw, path / "capture.json", read_settings(DEFAULT_PAPER_CONFIG),
                    strategy, raw_strategy, ledger, path / "audit.sqlite3",
                )
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["hourly_shadow"]["status"], "shadow_computation_failed")
        self.assertFalse(result["hourly_shadow"]["trade_eligible"])
        ledger.buy.assert_called_once()
        ledger.record_cycle.assert_called_once()


if __name__ == "__main__":
    unittest.main()
