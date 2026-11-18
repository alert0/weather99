import unittest
from copy import deepcopy
from unittest.mock import patch

from weather99.domain import MODELS, ForecastEstimate, timestamp
from weather99.nowcast import correct_forecast, realtime_sources
from weather99.qweather import QWeatherConfig, collect_daily_forecast


class NowcastTests(unittest.TestCase):
    def setUp(self):
        self.at = "2026-11-18T12:30:00+08:00"
        self.raw = {"collected_at": self.at, "timezone": "Asia/Shanghai",
                    "forecast": {"date": "2026-11-18"}, "sources": {
                        "observation": {"data": [{"icaoId": "ZSPD", "obsTime": timestamp(self.at).timestamp(),
                                                  "temp": 99, "rawOb": "ZSPD 180430Z 00000KT CAVOK 22/10 Q1020 RMK T02200100"}]},
                        "forecast": {"data": {"hourly": {"time": ["2026-11-18T12:00", "2026-11-18T13:00"],
                            **{f"temperature_2m_{m}": [19, 21] for m in MODELS}}}}},
                    "auxiliary_weather": {"qweather": {"complete": True, "summary": {
                        "current_times_match": True, "current_valid_times": [self.at],
                        "current_c": {"mean": 24, "min": 23, "max": 25}}}}}
        self.forecast = ForecastEstimate(25, 25, 25, 1.5, 1, "base", "baseline", self.at)

    def test_alignment_precise_metar_and_weighted_correction(self):
        result, detail = correct_forecast(self.raw, self.forecast, 22)
        self.assertEqual(detail["sources"]["metar"]["temperature_c"], 22)
        self.assertEqual(detail["sources"]["metar"]["model_current_c"], 20)
        self.assertEqual(result.center_c, 26.25)
        self.assertEqual(result.sigma_c, 1.5)
        self.assertNotEqual(result.model_version, "base")
        self.assertEqual(self.forecast.center_c, 25)

    def test_other_day_and_stale_data_cannot_correct(self):
        self.raw["forecast"]["date"] = "2026-11-19"
        self.assertEqual(correct_forecast(self.raw, self.forecast)[0], self.forecast)
        self.raw["forecast"]["date"] = "2026-11-18"
        self.raw["collected_at"] = "2026-11-18T14:00:00+08:00"
        self.assertEqual(correct_forecast(self.raw, self.forecast)[1]["status"], "no_aligned_observations")

    def test_missing_model_partial_points_future_and_limit(self):
        raw = deepcopy(self.raw)
        del raw["sources"]["forecast"]["data"]["hourly"][f"temperature_2m_{MODELS[0]}"]
        self.assertEqual(correct_forecast(raw, self.forecast)[0], self.forecast)
        self.raw["auxiliary_weather"]["qweather"]["complete"] = False
        self.assertEqual(correct_forecast(self.raw, self.forecast)[0].center_c, 25.75)
        self.raw["sources"]["observation"]["data"][0]["rawOb"] = "ZSPD 180430Z 00000KT CAVOK 40/10 Q1020"
        self.assertEqual(correct_forecast(self.raw, self.forecast)[0].center_c, 27)
        self.assertEqual(correct_forecast(self.raw, self.forecast, 40)[0].center_c, 40)
        self.raw["auxiliary_weather"]["qweather"]["complete"] = True
        self.raw["auxiliary_weather"]["qweather"]["summary"]["current_valid_times"] = ["2026-11-18T12:40:00+08:00"]
        self.assertEqual(realtime_sources(self.raw)["qweather"]["status"], "future")

    def test_daily_forecast_uses_one_center_request_and_provider_intervals(self):
        payload = {"metadata": {"attributions": ["provider"]}, "days": [{
            "forecastStartTime": "2026-11-17T22:00:00Z", "forecastEndTime": "2026-11-18T22:00:00Z",
            "temperatureMax": {"value": 20, "unit": "°C"},
            "temperatureMin": {"value": 12, "unit": "°C"}}]}
        with patch("weather99.qweather.fetch", return_value={"data": payload, "fetched_at": self.at}) as fetch:
            result = collect_daily_forecast(QWeatherConfig("test.qweatherapi.com", "secret"))
        fetch.assert_called_once()
        self.assertIn("/daily/31.1443/121.8083", fetch.call_args.args[0])
        self.assertEqual(result["days"][0]["date"], "2026-11-18")
        self.assertTrue(result["complete"])
        payload["days"][0]["temperatureMax"]["unit"] = "°F"
        with patch("weather99.qweather.fetch", return_value={"data": payload}):
            self.assertFalse(collect_daily_forecast(QWeatherConfig("test.qweatherapi.com", "secret"))["complete"])
