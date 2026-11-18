import unittest
from copy import deepcopy
from unittest.mock import patch

from tests.test_qweather import provider_response
from weather99.cli import weather_report
from weather99.locations import PUDONG_SAMPLING_POINTS
from weather99.qweather import (
    QWeatherConfig,
    airport_weather_for_date,
    collect_airport_qweather,
    summarize_points,
)


class AirportSamplingTests(unittest.TestCase):
    def test_five_distinct_points_and_ten_requests_with_raw_data_retained(self):
        with patch("weather99.qweather.fetch", side_effect=provider_response) as network:
            result = collect_airport_qweather(
                QWeatherConfig("test.qweatherapi.com", "key"), "2026-11-17"
            )
        self.assertEqual(network.call_count, 10)
        self.assertTrue(result["complete"])
        self.assertEqual(len(result["points"]), 5)
        coordinates = {
            (p["location"]["latitude"], p["location"]["longitude"]) for p in result["points"]
        }
        self.assertEqual(len(coordinates), 5)
        self.assertEqual(
            {p.id for p in PUDONG_SAMPLING_POINTS}, {p["point_id"] for p in result["points"]}
        )
        self.assertEqual(result["summary"]["forecast_max_c"]["points"], 5)
        self.assertEqual(result["summary"]["forecast_max_c"]["spread"], 0)
        self.assertFalse(result["summary"]["independent_models"])
        self.assertTrue(all(p["location"]["reference_station"] == "ZSPD" for p in result["points"]))
        self.assertTrue(all("sources" in p for p in result["points"]))
        self.assertTrue(all("sources" not in p for p in weather_report(result)["points"]))

    def fixture(self):
        with patch("weather99.qweather.fetch", side_effect=provider_response):
            return collect_airport_qweather(
                QWeatherConfig("test.qweatherapi.com", "key"), "2026-11-17"
            )

    def test_spatial_ranges_and_means_are_reported_for_matching_periods(self):
        points = self.fixture()["points"]
        for point, maximum in zip(points, (19, 20, 21, 22, 23)):
            point["forecast"]["temperature_max_c"] = maximum
        summary = summarize_points(points)
        self.assertEqual(
            summary["forecast_max_c"], {"min": 19, "max": 23, "mean": 21, "spread": 4, "points": 5}
        )

    def test_failed_point_is_explicit_and_valid_points_remain_usable(self):
        def partial_response(url, params=None, *, headers=None):
            if "/31.1543/121.8083" in url:
                return {"status": 503, "error": "HTTP 503", "url": url}
            return provider_response(url, params, headers=headers)

        with patch("weather99.qweather.fetch", side_effect=partial_response):
            result = collect_airport_qweather(
                QWeatherConfig("test.qweatherapi.com", "key"), "2026-11-17"
            )
        self.assertFalse(result["complete"])
        self.assertEqual(set(result["errors"]), {"north"})
        self.assertEqual(result["summary"]["complete_points"], 4)
        self.assertEqual(result["summary"]["forecast_max_c"]["points"], 4)

    def test_different_time_or_daily_period_is_not_a_spatial_temperature_difference(self):
        points = deepcopy(self.fixture()["points"])
        points[0]["current"]["observed_at"] = "2026-11-17T11:45+08:00"
        points[0]["forecast"]["period_start"] = "2026-11-17T07:00+08:00"
        summary = summarize_points(points)
        self.assertFalse(summary["current_times_match"])
        self.assertIsNone(summary["current_c"])
        self.assertFalse(summary["forecast_periods_match"])
        self.assertIsNone(summary["forecast_max_c"])

    def test_tomorrow_reuses_the_same_raw_responses_without_network_or_mutating_today(self):
        bundle = self.fixture()
        for point in bundle["points"]:
            tomorrow = deepcopy(point["sources"]["daily"]["data"]["days"][0])
            tomorrow.update(
                forecastStartTime="2026-11-18T00:00+08:00", forecastEndTime="2026-11-19T00:00+08:00"
            )
            tomorrow["temperatureMax"]["value"] = 25
            point["sources"]["daily"]["data"]["days"].append(tomorrow)
        with patch("weather99.qweather.fetch") as network:
            result = airport_weather_for_date(bundle, "2026-11-18")
        network.assert_not_called()
        self.assertEqual(result["summary"]["forecast_max_c"]["mean"], 25)
        self.assertEqual(bundle["target_date"], "2026-11-17")
        self.assertEqual(bundle["points"][0]["forecast"]["temperature_max_c"], 20.5)
        self.assertEqual(
            result["summary"]["current_valid_times"], bundle["summary"]["current_valid_times"]
        )

    def test_missing_day_is_not_replaced_with_the_previous_days_forecast(self):
        result = airport_weather_for_date(self.fixture(), "2026-11-18")
        self.assertFalse(result["complete"])
        self.assertEqual(result["summary"]["forecast_points"], 0)
        self.assertIsNone(result["summary"]["forecast_max_c"])


if __name__ == "__main__":
    unittest.main()
