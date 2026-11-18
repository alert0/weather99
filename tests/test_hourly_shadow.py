"""Point-in-time, source-provenance and no-trading checks for hourly shadow."""

import unittest
from copy import deepcopy
from dataclasses import replace
from datetime import datetime

from weather99.domain import MODELS, ForecastEstimate
from weather99.hourly_nowcast import hourly_tmax_shadow


class HourlyShadowTests(unittest.TestCase):
    def setUp(self):
        at = "2026-11-18T10:45:00+08:00"
        metar_time = datetime.fromisoformat("2026-11-18T10:30:00+08:00").timestamp()
        self.raw = {
            "collected_at": at,
            "timezone": "Asia/Shanghai",
            "forecast": {"date": "2026-11-18"},
            "sources": {
                "observation": {"data": [{
                    "icaoId": "ZSPD",
                    "obsTime": metar_time,
                    "rawOb": "METAR ZSPD 180230Z 00000KT 20/14 RMK T02000140",
                }]},
                "forecast": {"data": {"hourly": {
                    "time": [f"2026-11-18T{hour:02d}:00" for hour in range(24)],
                    **{f"temperature_2m_{m}": [17] * 10 + [18, 19, 20, 21] + [17] * 10
                       for m in MODELS},
                }}},
            },
            "auxiliary_weather": {},
        }
        self.baseline = ForecastEstimate(21, 21, 21, 1.5, 0.5, "base", "baseline", at)

    def test_path_anchored_to_station_and_hourly_temperature(self):
        result = hourly_tmax_shadow(self.raw, self.baseline, 20)
        self.assertEqual(result["status"], "shadow_ready")
        self.assertFalse(result["trade_eligible"])
        self.assertEqual(len(result["hours"]), 13)
        self.assertAlmostEqual(result["metar_residual_c"], 1.5)
        self.assertGreater(result["predicted_final_high_c"], 21)
        self.assertGreater(result["hours"][0]["shift_c"], result["hours"][-1]["shift_c"])
        self.assertEqual(self.baseline.center_c, 21)

    def test_station_record_is_hard_lower_bound(self):
        result = hourly_tmax_shadow(self.raw, self.baseline, 26)
        self.assertEqual(result["predicted_final_high_c"], 26)

    def test_other_day_and_missing_station_do_not_use_grid_fallback(self):
        raw = deepcopy(self.raw)
        raw["forecast"]["date"] = "2026-11-19"
        self.assertEqual(hourly_tmax_shadow(raw, self.baseline, 20)["status"], "not_target_day")
        raw = deepcopy(self.raw)
        raw["sources"]["observation"]["data"] = []
        self.assertEqual(
            hourly_tmax_shadow(raw, self.baseline, 20)["status"],
            "metar_unavailable_or_unaligned",
        )

    def test_grid_only_complements_verified_metar(self):
        raw = deepcopy(self.raw)
        raw["auxiliary_weather"]["qweather"] = {
            "complete": True, "summary": {
                "current_times_match": True,
                "current_valid_times": ["2026-11-18T10:30:00+08:00"],
                "current_c": {"mean": 20.5, "min": 20.4, "max": 20.6},
            },
        }
        output = hourly_tmax_shadow(raw, self.baseline, 20)
        self.assertTrue(output["qweather_used"])
        self.assertGreater(output["combined_residual_c"], output["metar_residual_c"])
        raw["auxiliary_weather"]["qweather"]["summary"]["current_c"]["mean"] = 29
        output = hourly_tmax_shadow(raw, self.baseline, 20)
        self.assertFalse(output["qweather_used"])
        self.assertAlmostEqual(output["combined_residual_c"], 1.5)

    def test_incomplete_target_day_curve_fails_closed(self):
        for problem in ("missing future row", "missing past reading", "short model series",
                        "duplicate hour", "off-hour timestamp"):
            with self.subTest(problem=problem):
                raw = deepcopy(self.raw)
                hourly = raw["sources"]["forecast"]["data"]["hourly"]
                series = hourly[f"temperature_2m_{MODELS[0]}"]
                if problem == "missing future row":
                    for values in hourly.values():
                        values.pop(13)
                elif problem == "missing past reading":
                    series[9] = None
                elif problem == "short model series":
                    series.pop()
                elif problem == "duplicate hour":
                    for values in hourly.values():
                        values.append(values[13])
                    hourly["time"][-1] = "2026-11-18T05:00:00Z"
                else:
                    hourly["time"][13] = "2026-11-18T13:15"

                result = hourly_tmax_shadow(raw, self.baseline, 20)
                self.assertEqual(result["status"], "target_day_hourly_path_incomplete")
                self.assertEqual(result["hours"], [])
                self.assertNotIn("predicted_final_high_c", result)

    def test_model_available_after_capture_fails_closed(self):
        baseline = replace(self.baseline, available_at="2026-11-18T02:45:01Z")
        result = hourly_tmax_shadow(self.raw, baseline, 20)
        self.assertEqual(result["status"], "model_not_available_at_snapshot")
        self.assertNotIn("predicted_final_high_c", result)

    def test_stale_metar_fails_closed(self):
        raw = deepcopy(self.raw)
        raw["collected_at"] = "2026-11-18T13:45:00+08:00"
        self.assertEqual(
            hourly_tmax_shadow(raw, self.baseline, 20)["status"],
            "metar_unavailable_or_unaligned",
        )

    def test_result_is_deterministic_no_future_observation_included(self):
        first = hourly_tmax_shadow(self.raw, self.baseline, 20)
        second = hourly_tmax_shadow(self.raw, self.baseline, 20)
        self.assertEqual(first, second)
        raw = deepcopy(self.raw)
        raw["sources"]["observation"]["data"].append({
            "icaoId": "ZSPD",
            "obsTime": datetime.fromisoformat("2026-11-18T11:30:00+08:00").timestamp(),
            "rawOb": "METAR ZSPD 180330Z 00000KT 29/14 RMK T02900140",
        })
        self.assertEqual(hourly_tmax_shadow(raw, self.baseline, 20), first)


if __name__ == "__main__":
    unittest.main()
