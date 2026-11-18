import unittest
from copy import deepcopy
from datetime import timedelta

from tests.test_forecasting import synthetic_history
from weather99.adapters import live_snapshot
from weather99.domain import MODELS, timestamp
from weather99.forecasting import fit


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.model = fit(synthetic_history(), "2026-09-24T08:00:00+08:00")
        as_of = "2026-09-24T12:00:00+08:00"
        slug = "highest-temperature-in-shanghai-on-september-24-2026"
        self.capture = {
            "schema": "weather99.snapshot.v1",
            "station": "ZSPD",
            "event_slug": slug,
            "collected_at": as_of,
            "errors": {},
            "forecast": {"date": "2026-09-24", "models_c": dict(zip(MODELS, (23, 24, 25, 26)))},
            "buckets": [
                {
                    "token_id": "a",
                    "bucket_label": "25°C or below",
                    "bid": 0.4,
                    "ask": 0.42,
                    "market_probability": 0.4,
                    "book_timestamp": timestamp(as_of).timestamp() * 1000,
                },
                {
                    "token_id": "b",
                    "bucket_label": "26°C or higher",
                    "bid": 0.58,
                    "ask": 0.6,
                    "market_probability": 0.6,
                    "book_timestamp": timestamp(as_of).timestamp() * 1000,
                },
            ],
            "sources": {
                "forecast": {"fetched_at": as_of},
                "event": {
                    "fetched_at": as_of,
                    "data": {
                        "slug": slug,
                        "active": True,
                        "closed": False,
                        "endDate": "2026-09-25T00:00:00+08:00",
                    },
                },
                "observation": {
                    "data": [
                        {
                            "icaoId": "ZSPD",
                            "obsTime": timestamp(as_of).timestamp() - 600,
                            "temp": 25,
                        },
                        {
                            "icaoId": "ZSPD",
                            "obsTime": timestamp(as_of).timestamp() - 7200,
                            "temp": 27,
                        },
                        {
                            "icaoId": "OTHER",
                            "obsTime": timestamp(as_of).timestamp() - 600,
                            "temp": 50,
                        },
                        {
                            "icaoId": "ZSPD",
                            "obsTime": timestamp(as_of).timestamp() + 600,
                            "temp": 40,
                        },
                        {
                            "icaoId": "ZSPD",
                            "obsTime": timestamp(as_of).timestamp() - 86400,
                            "temp": 35,
                        },
                    ]
                },
            },
        }

    def test_observation_lower_bound_uses_today_pudong_only(self):
        snapshot = live_snapshot(self.capture, self.model, "24h")
        self.assertEqual(snapshot.observed_max_c, 27)

    def test_stale_observations_rejected_for_today(self):
        self.capture["sources"]["observation"]["data"] = [
            {
                "icaoId": "ZSPD",
                "obsTime": timestamp(self.capture["collected_at"]).timestamp() - 7200,
                "temp": 27,
            }
        ]
        with self.assertRaisesRegex(ValueError, "no older than one hour"):
            live_snapshot(self.capture, self.model, "24h")

    def test_verified_settlement_temperature_drives_lower_bound(self):
        self.capture['settlement'] = {
            'rules_verified': True, 'observed_max_c': 26.5,
            'latest_observed_at': self.capture['collected_at'],
        }
        self.assertEqual(live_snapshot(self.capture, self.model, '24h').observed_max_c, 26.5)
        self.capture['settlement']['observed_max_c'] = None
        with self.assertRaisesRegex(ValueError, 'settlement_evidence_missing'):
            live_snapshot(self.capture, self.model, '24h')

    def test_horizon_station_source_errors_and_missing_models_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "horizon"):
            live_snapshot(self.capture, self.model, "3h")
        for change, expected in (
            ({"station": "ZSSS"}, "station"),
            ({"errors": {"event": "451"}}, "failed sources"),
        ):
            with self.assertRaisesRegex(ValueError, expected):
                live_snapshot(self.capture | change, self.model, "24h")
        del self.capture["forecast"]["models_c"][MODELS[0]]
        with self.assertRaisesRegex(ValueError, "all four"):
            live_snapshot(self.capture, self.model, "24h")

    def test_missing_book_time_and_stale_market_probability_are_rejected(self):
        capture = deepcopy(self.capture)
        capture["buckets"][0]["book_timestamp"] = None
        with self.assertRaisesRegex(ValueError, "book timestamp"):
            live_snapshot(capture, self.model, "24h")
        self.capture["sources"]["event"]["fetched_at"] = (
            timestamp(self.capture["collected_at"]) - timedelta(minutes=6)
        ).isoformat()
        with self.assertRaisesRegex(ValueError, "probability source timestamp"):
            live_snapshot(self.capture, self.model, "24h")


if __name__ == "__main__":
    unittest.main()
