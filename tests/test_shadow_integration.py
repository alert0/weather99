"""A failing diagnostic shadow cannot block normal Paper evaluation."""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from tests.test_strategy import scenario
from weather99.config import load_config
from weather99.domain import MODELS
from weather99.paper import DEFAULT_PAPER_CONFIG, process_capture, read_settings


class ShadowIsolationTests(unittest.TestCase):
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
