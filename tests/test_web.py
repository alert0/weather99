import http.client
import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from tempfile import TemporaryDirectory
from pathlib import Path

from weather99.domain import MODELS, timestamp
from weather99.web import handler_for, scan_view


class DashboardHTTPTests(unittest.TestCase):
    def test_page_and_settlement_module_are_served_with_dashboard(self):
        with TemporaryDirectory() as folder:
            with ThreadingHTTPServer(("127.0.0.1", 0), handler_for(Path(folder))) as server:
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    for route, expected in (("/", b"settlement-summary"),
                                            ("/hourly.js", b"renderHourly"),
                                            ("/settlement.js", b"renderSettlement"),
                                            ("/api/dashboard", b"weather99.dashboard.v1")):
                        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
                        connection.request("GET", route)
                        response = connection.getresponse()
                        self.assertEqual(response.status, 200, route)
                        self.assertIn(expected, response.read())
                        connection.close()
                finally:
                    server.shutdown()
                    thread.join(timeout=3)

    def test_market_failure_still_exposes_target_day_hourly_weather(self):
        target = "2026-11-18"
        captured_at = f"{target}T10:45:00+08:00"
        observed_at = timestamp(f"{target}T10:30:00+08:00")
        raw = {
            "collected_at": captured_at,
            "timezone": "Asia/Shanghai",
            "errors": {"event": "request failed"},
            "sources": {
                "forecast": {"fetched_at": captured_at, "data": {"hourly": {
                    "time": [f"{target}T{hour:02}:00" for hour in range(24)],
                    **{f"temperature_2m_{model}": [18 + index] * 24
                       for index, model in enumerate(MODELS)},
                }}},
                "observation": {"data": [{
                    "icaoId": "ZSPD", "obsTime": observed_at.timestamp(),
                    "rawOb": "METAR ZSPD 180230Z 00000KT 20/14",
                }]},
            },
        }
        with TemporaryDirectory() as folder:
            path = Path(folder) / "capture.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            scan = scan_view(
                {"target_date": target, "status": "blocked", "snapshot": path}, path.parent
            )

        self.assertTrue(scan["market_source_failed"])
        self.assertEqual(scan["quotes"], [])
        self.assertIsNone(scan["hourly_shadow"])
        self.assertEqual(scan["hourly"]["forecast_at"], captured_at)
        self.assertEqual(len(scan["hourly"]["hours"]), 24)
        self.assertEqual(scan["hourly"]["hours"][0], {
            "valid_at": f"{target}T00:00:00+08:00", "model_mean_c": 19.5,
        })
        self.assertEqual(scan["hourly"]["observations"], [{
            "observed_at": "2026-11-18T02:30:00+00:00", "temperature_c": 20,
        }])
