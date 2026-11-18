import gzip
import io
import json
import os
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from weather99.cli import main
from weather99.http import NoCredentialRedirect, fetch
from weather99.ingest import collect
from weather99.qweather import QWeatherConfig, collect_qweather


def provider_response(url, params=None, *, headers=None):
    if "/current/" not in url:
        raise AssertionError(f"unexpected QWeather endpoint: {url}")
    payload = {
        "validTime": "2026-11-17T12:00+08:00",
        "temperature": {"value": 19, "unit": "°C"},
        "metadata": {"attributions": ["https://developer.qweather.com/attribution.html"]},
    }
    return {"url": url, "status": 200, "data": payload, "raw": json.dumps(payload)}


class QWeatherTests(unittest.TestCase):
    def test_environment_is_optional_but_partial_or_ambiguous_configuration_fails(self):
        with (
            patch.dict(os.environ, {}, clear=True),
            patch(
                "weather99.qweather.LOCAL_CREDENTIALS", Path("runtime/nonexistent-credentials.json")
            ),
        ):
            self.assertIsNone(QWeatherConfig.from_env())
            with self.assertRaisesRegex(ValueError, "set QWEATHER"):
                QWeatherConfig.from_env(required=True)
        with self.assertRaisesRegex(ValueError, "exactly one"):
            QWeatherConfig("test.qweatherapi.com", "key", "jwt")
        for host in (
            "http://test.qweatherapi.com",
            "test.qweatherapi.com.evil.com",
            "https://user:pass@test.qweatherapi.com",
            "test.qweatherapi.com/path",
        ):
            with self.assertRaisesRegex(ValueError, "dedicated"):
                QWeatherConfig(host, "key")

    def test_secret_is_header_only_not_in_repr_or_snapshot(self):
        config = QWeatherConfig("https://test.xy.qweatherapi.com/", "secret-key")
        self.assertNotIn("secret-key", repr(config))
        with patch("weather99.qweather.fetch", side_effect=provider_response) as transport:
            result = collect_qweather(config, "2026-11-17")
        self.assertTrue(result["complete"])
        self.assertEqual(result["location"]["reference_station"], "ZSPD")
        self.assertEqual(result["location"]["name"], "上海浦东国际机场")
        self.assertNotIn("secret-key", json.dumps(result))
        for call in transport.call_args_list:
            self.assertEqual(call.kwargs["headers"], {"X-QW-Api-Key": "secret-key"})
            self.assertIn("/31.1443/121.8083", call.args[0])
        self.assertEqual(result["current"]["temperature_c"], 19)
        transport.assert_called_once()
        self.assertFalse(result["current"]["station_verified"])
        self.assertEqual(len(result["attributions"]), 1)
        self.assertEqual(
            QWeatherConfig("test.qweatherapi.com", jwt="jwt-token").auth_headers(),
            {"Authorization": "Bearer jwt-token"},
        )

    def test_business_errors_are_not_successful_weather(self):
        with patch("weather99.qweather.fetch", return_value={"data": {"code": "401"}}):
            result = collect_qweather(
                QWeatherConfig("test.qweatherapi.com", "key"), "2026-11-17", 31, 121
            )
        self.assertFalse(result["complete"])
        self.assertIsNone(result["location"]["reference_station"])
        self.assertIsNone(result["current"])
        self.assertEqual(set(result["errors"]), {"current"})

    def test_current_temperature_requires_celsius(self):
        response = provider_response("https://test.qweatherapi.com/current/31/121")
        response["data"]["temperature"]["unit"] = "°F"
        with patch("weather99.qweather.fetch", return_value=response):
            result = collect_qweather(QWeatherConfig("test.qweatherapi.com", "key"), "2026-11-17")
        self.assertIsNone(result["current"])
        self.assertFalse(result["complete"])
        self.assertIn("current", result["errors"])

    def test_provider_failure_is_local_and_does_not_masquerade_as_model_or_metar(self):
        config = QWeatherConfig("test.qweatherapi.com", "key")
        auxiliary = {"complete": False, "errors": {"current": "HTTP 401"}}
        with (
            TemporaryDirectory() as directory,
            patch("weather99.ingest.fetch", return_value={"data": {}}),
            patch("weather99.ingest.collect_airport_qweather", return_value=auxiliary),
        ):
            _, result = collect("2026-11-17", Path(directory), config)
        self.assertEqual(result["auxiliary_weather"]["qweather"], auxiliary)
        self.assertEqual(result["errors"], {})
        self.assertFalse(result["forecast"]["all_four_models"])
        self.assertNotIn("qweather", result["forecast"]["models_c"])

    def test_cli_without_credentials_fails_before_network(self):
        with (
            patch.dict(os.environ, {}, clear=True),
            patch(
                "weather99.qweather.LOCAL_CREDENTIALS", Path("runtime/nonexistent-credentials.json")
            ),
            patch("weather99.qweather.fetch") as network,
            redirect_stdout(buffer := io.StringIO()),
        ):
            code = main(["qweather"])
        self.assertEqual(code, 2)
        self.assertIn("QWEATHER_API_HOST", json.loads(buffer.getvalue())["error"])
        network.assert_not_called()


class TransportTests(unittest.TestCase):
    def test_gzip_decode_and_redaction_on_authenticated_response(self):
        response = Mock(status=200, headers={"Content-Encoding": "gzip"})
        response.read.return_value = gzip.compress(b'{"test":"secret-key"}')
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=None)
        opener = Mock()
        opener.open.return_value = response
        with patch("weather99.http.build_opener", return_value=opener):
            result = fetch(
                "https://test.qweatherapi.com/weather/v1/current/31/121",
                headers={"X-QW-Api-Key": "secret-key"},
            )
        self.assertNotIn("secret-key", json.dumps(result))
        self.assertEqual(result["data"], {"test": "[REDACTED]"})

    def test_auth_failure_is_not_retried_or_exposed(self):
        opener = Mock()
        opener.open.side_effect = HTTPError(
            "https://test.qweatherapi.com", 401, "secret-key", {}, None
        )
        with (
            patch("weather99.http.build_opener", return_value=opener),
            patch("weather99.http.time.sleep") as sleep,
        ):
            result = fetch("https://test.qweatherapi.com", headers={"X-QW-Api-Key": "secret-key"})
        self.assertEqual(result["status"], 401)
        self.assertNotIn("secret-key", result["error"])
        opener.open.assert_called_once()
        sleep.assert_not_called()

    def test_credentials_cannot_follow_a_redirect(self):
        handler = NoCredentialRedirect()
        request = Mock(full_url="https://test.qweatherapi.com")
        with self.assertRaises(HTTPError) as caught:
            handler.redirect_request(request, None, 302, "redirect", {}, "https://other.example")
        caught.exception.close()


if __name__ == "__main__":
    unittest.main()
