import json
import unittest
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from tests.test_strategy import scenario
from weather99.config import load_config
from weather99.domain import MODELS, timestamp
from weather99.network import proxy_url
from weather99.paper import (
    DEFAULT_PAPER_CONFIG,
    PaperBaseline,
    SingleInstance,
    process_capture,
    read_settings,
)
from weather99.paper_ledger import PaperLedger, depth_value
from weather99.storage import AuditStore
from weather99.strategy import evaluate


class PaperLedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.ledger = PaperLedger(self.root / "paper.sqlite3")
        self.snapshot, self.risk = scenario()
        self.strategy, self.strategy_raw = load_config()
        self.settings = read_settings(DEFAULT_PAPER_CONFIG)
        self.raw = {
            "collected_at": self.snapshot.as_of,
            "event_slug": "test-event",
            "forecast": {"date": self.snapshot.target_date},
            "sources": {},
        }
        for bucket in self.snapshot.buckets:
            self.raw["sources"][f"book:{bucket.key}"] = {
                "data": {
                    "timestamp": str(int(timestamp(self.snapshot.as_of).timestamp() * 1000)),
                    "min_order_size": "5",
                    "asks": [{"price": str(bucket.ask), "size": "20"}],
                    "bids": [{"price": str(bucket.bid), "size": "20"}],
                }
            }
        self.signal = evaluate(self.snapshot, self.strategy, self.risk).to_dict() | {
            "id": "confirmed-test-signal",
            "status": "confirmed_research_signal",
        }

    def tearDown(self):
        self.ledger.db.close()
        self.temp.cleanup()

    def buy(self, raw=None, signal=None, settings=None):
        return self.ledger.buy(
            signal or self.signal,
            self.snapshot,
            raw or self.raw,
            settings or self.settings,
            self.strategy,
        )

    def test_cash_reserve_mark_and_no_duplicate_market_entry(self):
        result = self.buy()
        self.assertEqual(result["status"], "paper_filled")
        self.assertEqual(result["cost_usd"], 1.25)
        self.assertEqual(result["reserve_usd"], 0.075)
        self.ledger.mark(self.raw)
        account = self.ledger.account(self.snapshot.as_of)
        self.assertEqual(account["cash_usd"], 48.675)
        self.assertEqual(account["equity_usd"], 49.875)
        self.assertTrue(account["valuation_complete"])
        self.assertEqual(account["daily_realized_usd"], -0.075)
        self.assertEqual(self.buy()["reason"], "market_already_traded")
        self.assertEqual(self.ledger.account(self.snapshot.as_of)["cash_usd"], 48.675)

    def test_confirmation_depth_minimum_and_budget_checks(self):
        self.assertEqual(
            self.buy(signal=self.signal | {"status": "pending_confirmation"})["reason"],
            "signal_not_confirmed",
        )
        raw = deepcopy(self.raw)
        raw["sources"]["book:18"]["data"]["asks"][0]["size"] = "4"
        self.assertEqual(self.buy(raw=raw)["reason"], "insufficient_ask_depth")
        raw["sources"]["book:18"]["data"]["min_order_size"] = "10"
        self.assertEqual(self.buy(raw=raw)["reason"], "below_book_min_order_size")
        self.assertEqual(
            self.buy(settings=self.settings | {"position_budget_usd": 1})["reason"],
            "position_budget_exceeded",
        )
        raw = deepcopy(self.raw)
        raw["sources"]["book:18"]["data"]["asks"] = [{"price": "0.9", "size": "100"}]
        self.assertEqual(self.buy(raw=raw)["reason"], "depth_adjusted_edge_below_threshold")
        self.assertEqual(self.ledger.account(self.snapshot.as_of)["cash_usd"], 50)

    def test_depth_fill_uses_all_levels_in_price_order(self):
        book = {"asks": [{"price": "0.4", "size": "3"}, {"price": "0.2", "size": "2"}]}
        self.assertEqual(depth_value(book, "asks", 5), 1_600_000)
        self.assertIsNone(depth_value(book, "asks", 6))

    def resolved_event(self, *, winner="18", status="resolved"):
        raw = deepcopy(self.raw)
        raw["collected_at"] = "2026-11-18T01:00:00+08:00"
        raw["sources"]["event"] = {
            "data": {
                "slug": "test-event",
                "closed": True,
                "markets": [
                    {
                        "closed": True,
                        "umaResolutionStatus": status,
                        "clobTokenIds": json.dumps([b.key, "no-" + b.key]),
                        "outcomes": '["Yes", "No"]',
                        "outcomePrices": json.dumps([int(b.key == winner), int(b.key != winner)]),
                    }
                    for b in self.snapshot.buckets
                ],
            }
        }
        return raw

    def test_settlement_requires_final_resolution_and_is_idempotent(self):
        self.buy()
        self.assertEqual(self.ledger.settle(self.resolved_event(status="proposed")), [])
        settled = self.ledger.settle(self.resolved_event())
        self.assertEqual(settled[0]["payout_usd"], 5)
        self.assertEqual(settled[0]["net_pnl_usd"], 3.675)
        self.assertEqual(self.ledger.settle(self.resolved_event()), [])
        account = self.ledger.account("2026-11-18T02:00:00+08:00")
        self.assertEqual(account["cash_usd"], 53.675)
        self.assertEqual(account["open_positions"], 0)
        self.assertEqual(account["daily_realized_usd"], 3.75)

    def test_loss_and_missing_bid_do_not_create_fake_profit(self):
        self.buy()
        raw = deepcopy(self.raw)
        raw["sources"]["book:18"]["data"]["bids"] = []
        self.ledger.mark(raw)
        account = self.ledger.account(self.snapshot.as_of)
        self.assertFalse(account["valuation_complete"])
        self.assertEqual(account["equity_usd"], 48.675)
        self.ledger.settle(self.resolved_event(winner="high"))
        self.assertEqual(self.ledger.account("2026-11-18T02:00:00+08:00")["consecutive_losses"], 1)

    def test_restarting_keeps_balance_and_cannot_reset_initial_cash(self):
        self.buy()
        with PaperLedger(self.root / "paper.sqlite3") as restarted:
            self.assertEqual(restarted.account(self.snapshot.as_of)["cash_usd"], 48.675)
        with self.assertRaisesRegex(ValueError, "cannot be changed"):
            PaperLedger(self.root / "paper.sqlite3", 100)


class PaperRunnerTests(unittest.TestCase):
    def test_baseline_has_stable_identity_and_is_explicitly_uncalibrated(self):
        model = PaperBaseline("24h", "2026-11-16T12:00:00+08:00", 1.5)
        later = replace(model, available_at="2026-11-16T12:15:00+08:00")
        self.assertEqual(model.version, later.version)
        forecast = model.predict(dict(zip(MODELS, (17, 18, 19, 20))))
        self.assertEqual(forecast.center_c, 18.5)
        self.assertIn("uncalibrated", forecast.method)
        self.assertGreater(forecast.sigma_c, 1.5)

    def test_invalid_scan_breaks_confirmation_without_generating_a_fill(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with PaperLedger(root / "paper.sqlite3") as ledger:
                strategy, raw_strategy = load_config()
                raw = {
                    "collected_at": "2026-11-16T12:00:00+08:00",
                    "event_slug": "test",
                    "forecast": {"date": "2026-11-17"},
                    "sources": {},
                    "errors": {"event": "proxy failed"},
                }
                result = process_capture(
                    raw,
                    root / "capture.json",
                    read_settings(DEFAULT_PAPER_CONFIG),
                    strategy,
                    raw_strategy,
                    ledger,
                    root / "audit.sqlite3",
                )
                self.assertEqual(result["status"], "blocked")
                self.assertEqual(ledger.positions(), [])
                with AuditStore(root / "audit.sqlite3") as audit:
                    state = audit.connection.execute(
                        "SELECT count FROM confirmation_state"
                    ).fetchone()
                    self.assertEqual(state["count"], 0)

    def test_single_instance_lock_prevents_two_writers(self):
        with (
            TemporaryDirectory() as directory,
            SingleInstance(Path(directory) / "lock"),
            self.assertRaisesRegex(ValueError, "already"),
            SingleInstance(Path(directory) / "lock"),
        ):
            self.fail("second runner acquired lock")

    def test_explicit_proxy_overrides_local_configuration(self):
        with patch.dict("os.environ", {"WEATHER99_PROXY_URL": "http://127.0.0.1:20808"}):
            self.assertEqual(proxy_url(), "http://127.0.0.1:20808")
