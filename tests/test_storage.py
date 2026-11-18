import unittest
from dataclasses import asdict, replace
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from tests.test_strategy import scenario
from weather99.config import load_config
from weather99.domain import timestamp
from weather99.storage import AuditStore
from weather99.strategy import evaluate


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.store = AuditStore(Path(self.directory.name) / "audit.sqlite3")
        self.config, self.raw_config = load_config()
        self.snapshot, self.risk = scenario()

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def record(self, snapshot, risk=None):
        risk = self.risk if risk is None else risk
        result = evaluate(snapshot, self.config, risk)
        return self.store.record(
            snapshot, result, self.config, risk, self.raw_config, asdict(snapshot)
        )

    def next_snapshot(self, minutes=1):
        as_of = (timestamp(self.snapshot.as_of) + timedelta(minutes=minutes)).isoformat()
        return replace(
            self.snapshot,
            snapshot_id=f"minute-{minutes}",
            as_of=as_of,
            buckets=tuple(replace(b, quoted_at=as_of) for b in self.snapshot.buckets),
        )

    def test_distinct_scans_confirm_and_replay_does_not_count_twice(self):
        first = self.record(self.snapshot)
        self.assertEqual(first["status"], "pending_confirmation")
        self.assertEqual(first, self.record(self.snapshot))
        second = self.record(self.next_snapshot())
        self.assertEqual(second["confirmation_count"], 2)
        self.assertEqual(second["status"], "confirmed_research_signal")
        self.assertFalse(second["execution_enabled"])
        self.assertEqual(
            self.store.connection.execute("SELECT count(*) FROM evaluations").fetchone()[0], 2
        )
        self.assertEqual(
            self.store.connection.execute("SELECT count(*) FROM domain_events").fetchone()[0], 2
        )

    def test_rejection_resets_and_time_gap_expires_confirmation(self):
        self.record(self.snapshot)
        veto = self.record(self.next_snapshot(1), replace(self.risk, daily_realized_usd=-5))
        self.assertEqual(veto["confirmation_count"], 0)
        after = self.record(self.next_snapshot(2))
        self.assertEqual(after["confirmation_count"], 1)
        expired = self.record(self.next_snapshot(40))
        self.assertEqual(expired["confirmation_count"], 1)

    def test_out_of_order_or_equal_timestamp_cannot_confirm(self):
        self.record(self.next_snapshot(2))
        old = self.record(self.snapshot)
        self.assertEqual(old["status"], "rejected")
        self.assertIn("non_increasing_snapshot_time", old["reasons"])
        self.assertFalse(old["eligible"])
        equal = self.record(replace(self.next_snapshot(2), snapshot_id="different-id-same-time"))
        self.assertEqual(equal["status"], "rejected")
        current = self.record(self.next_snapshot(3))
        self.assertEqual(current["confirmation_count"], 2)

    def test_changed_model_or_configuration_resets_confirmation(self):
        self.record(self.snapshot)
        changed = replace(
            self.next_snapshot(), forecast=replace(self.snapshot.forecast, model_version="other")
        )
        self.assertEqual(self.record(changed)["confirmation_count"], 1)

    def test_artifacts_are_immutable(self):
        self.store.register("test", "test", {"x": 1})
        with self.assertRaisesRegex(ValueError, "immutable"):
            self.store.register("test", "test", {"x": 2})


if __name__ == "__main__":
    unittest.main()
