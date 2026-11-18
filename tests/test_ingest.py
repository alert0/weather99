import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from weather99.ingest import (
    MODELS,
    best_prices,
    city_metadata,
    collect,
    forecast_summary,
    yes_token,
)


class IngestionTests(unittest.TestCase):
    def test_four_models_must_be_complete_for_equal_weight_center(self):
        daily = {"time": ["2026-11-17"]}
        for model, value in zip(MODELS, (18, 17, 16, 19)):
            daily[f"temperature_2m_max_{model}"] = [value]
        self.assertEqual(
            forecast_summary({"daily": daily}, "2026-11-17")["raw_equal_weight_center_c"], 17.5
        )
        daily[f"temperature_2m_max_{MODELS[-1]}"] = [None]
        result = forecast_summary({"daily": daily}, "2026-11-17")
        self.assertFalse(result["all_four_models"])
        self.assertIsNone(result["raw_equal_weight_center_c"])
        self.assertFalse(forecast_summary({"daily": daily}, "2026-11-18")["all_four_models"])

    def test_yes_token_follows_outcome_order(self):
        self.assertEqual(
            yes_token({"outcomes": '["No", "Yes"]', "clobTokenIds": '["no-token", "yes-token"]'}),
            "yes-token",
        )
        self.assertIsNone(yes_token({"outcomes": ["Yes"], "clobTokenIds": []}))

    def test_orderbook_is_not_assumed_sorted(self):
        result = best_prices(
            {
                "bids": [{"price": "0.1", "size": "1"}, {"price": "0.6", "size": "2"}],
                "asks": [
                    {"price": "0.9", "size": "1"},
                    {"price": "0.7", "size": "3"},
                    {"price": "0.61", "size": "0"},
                ],
            }
        )
        self.assertEqual(result["bid"], 0.6)
        self.assertEqual(result["ask"], 0.7)
        self.assertTrue(result["two_sided"])

    def test_one_sided_or_crossed_books_are_not_valid(self):
        self.assertFalse(best_prices({"asks": [{"price": "0.2", "size": "1"}]})["two_sided"])
        self.assertFalse(
            best_prices(
                {"bids": [{"price": "0.8", "size": "1"}], "asks": [{"price": "0.2", "size": "1"}]}
            )["two_sided"]
        )

    def test_pinned_upstream_registry_has_pudong_station(self):
        self.assertEqual(city_metadata()["icao"], "ZSPD")

    def test_failed_market_still_persists_weather_snapshot_without_overwrite(self):
        def response(url, params=None):
            if "gamma-api" in url:
                return {"url": url, "error": "HTTP 451", "status": 451}
            if "open-meteo" in url:
                daily = {"time": ["2026-11-17"]}
                daily.update({f"temperature_2m_max_{model}": [17] for model in MODELS})
                return {"url": url, "data": {"daily": daily}, "raw": "forecast"}
            return {"url": url, "data": [{"icaoId": "ZSPD", "temp": 17}]}

        with (
            TemporaryDirectory() as directory,
            patch("weather99.ingest.fetch", side_effect=response),
        ):
            path, snapshot = collect("2026-11-17", Path(directory))
            second_path, _ = collect("2026-11-17", Path(directory))
            self.assertNotEqual(path, second_path)
            self.assertFalse(snapshot["data_ready"])
            self.assertTrue(snapshot["forecast"]["all_four_models"])
            self.assertEqual(snapshot["errors"], {"event": "HTTP 451"})
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), snapshot)


if __name__ == "__main__":
    unittest.main()
