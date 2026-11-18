"""QWeather grid weather provider; kept separate from model and settlement inputs."""

import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from statistics import fmean

from .domain import SHANGHAI, finite, timestamp
from .http import fetch
from .locations import PUDONG, PUDONG_SAMPLING_POINTS
from .qweather_auth import JWTSigner

LOCAL_CREDENTIALS = Path(__file__).resolve().parents[1] / "runtime/credentials/qweather.json"


@dataclass(frozen=True)
class QWeatherConfig:
    host: str
    api_key: str = field(default="", repr=False)
    jwt: str = field(default="", repr=False)
    signer: JWTSigner | None = field(default=None, repr=False)

    def __post_init__(self):
        host = self.host.strip().removeprefix("https://").rstrip("/")
        if not re.fullmatch(r"[a-z0-9-]+(?:\.[a-z0-9-]+)*\.qweatherapi\.com", host):
            raise ValueError(
                "QWEATHER_API_HOST must be your dedicated *.qweatherapi.com HTTPS host"
            )
        object.__setattr__(self, "host", host)
        if sum((bool(self.api_key), bool(self.jwt), self.signer is not None)) != 1:
            raise ValueError("configure exactly one of QWEATHER_API_KEY or QWEATHER_JWT")
        credential = self.api_key or self.jwt
        if any(char.isspace() for char in credential):
            raise ValueError("QWeather credential cannot contain whitespace")

    @classmethod
    def from_env(cls, *, required=False):
        host = os.environ.get("QWEATHER_API_HOST", "").strip()
        key = os.environ.get("QWEATHER_API_KEY", "").strip()
        jwt = os.environ.get("QWEATHER_JWT", "").strip()
        if not any((host, key, jwt)) and not required:
            return cls.from_file(LOCAL_CREDENTIALS) if LOCAL_CREDENTIALS.exists() else None
        if not any((host, key, jwt)) and LOCAL_CREDENTIALS.exists():
            return cls.from_file(LOCAL_CREDENTIALS)
        if not host or not (key or jwt):
            raise ValueError("set QWEATHER_API_HOST and QWEATHER_API_KEY or QWEATHER_JWT locally")
        return cls(host, key, jwt)

    @classmethod
    def from_file(cls, path: Path):
        data = json.loads(path.read_text(encoding="utf-8"))
        key_file = (path.parent / data["private_key_file"]).resolve()
        signer = JWTSigner(
            data["developer_id"], data["project_id"], data["credential_id"], key_file
        )
        return cls(data["host"], signer=signer)

    def auth_headers(self):
        if self.signer:
            return {"Authorization": "Bearer " + self.signer.token()}
        return (
            {"Authorization": f"Bearer {self.jwt}"} if self.jwt else {"X-QW-Api-Key": self.api_key}
        )


def temperature_c(value):
    if not isinstance(value, dict):
        raise TypeError("QWeather temperature must be an object")
    if value.get("unit") != "°C":
        raise ValueError("QWeather temperature must be reported in Celsius")
    finite(value.get("value"), "QWeather temperature")
    return value["value"]


def normalize_daily(payload, target_date):
    day = date.fromisoformat(target_date)
    rows = payload.get("days")
    if not isinstance(rows, list):
        raise TypeError("QWeather daily response days must be an array")
    matches = []
    for row in rows:
        start, end = timestamp(row["forecastStartTime"]), timestamp(row["forecastEndTime"])
        if end <= start:
            raise ValueError("QWeather forecast period is reversed")
        local_start, local_end = start.astimezone(SHANGHAI), end.astimezone(SHANGHAI)
        if local_start.date() != day:
            continue
        matches.append(
            {
                "date": target_date,
                "temperature_max_c": temperature_c(row["temperatureMax"]),
                "temperature_min_c": temperature_c(row["temperatureMin"]),
                "period_start": start.isoformat(),
                "period_end": end.isoformat(),
                "calendar_day_aligned": local_start.time().isoformat() == "00:00:00"
                and local_end.time().isoformat() == "00:00:00"
                and local_end.date() == day + timedelta(days=1),
            }
        )
    if len(matches) != 1:
        raise ValueError("QWeather target date is missing or duplicated in daily forecast")
    if matches[0]["temperature_min_c"] > matches[0]["temperature_max_c"]:
        raise ValueError("QWeather minimum temperature exceeds maximum")
    return matches[0]


def normalize_current(payload):
    valid_time = timestamp(payload["validTime"])
    return {
        "observed_at": valid_time.isoformat(),
        "temperature_c": temperature_c(payload["temperature"]),
        "condition": payload.get("condition"),
        "station_verified": False,
    }


def collect_qweather(
    config: QWeatherConfig,
    target_date: str,
    latitude: float = PUDONG.latitude,
    longitude: float = PUDONG.longitude,
):
    """Two bounded read requests. Return errors as provider-local diagnostics."""
    date.fromisoformat(target_date)
    for value, name, limit in ((latitude, "latitude", 90), (longitude, "longitude", 180)):
        finite(value, name)
        if not -limit <= value <= limit:
            raise ValueError(f"invalid {name}")
    base = f"https://{config.host}/weather/v1"
    coordinates = f"{latitude}/{longitude}"
    jobs = {
        "daily": (f"{base}/daily/{coordinates}", {"days": 7, "localTime": "true", "lang": "zh"}),
        "current": (f"{base}/current/{coordinates}", {"localTime": "true", "lang": "zh"}),
    }
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {
            name: pool.submit(fetch, url, params, headers=config.auth_headers())
            for name, (url, params) in jobs.items()
        }
        sources = {name: future.result() for name, future in futures.items()}
    normalized, errors, attributions = {}, {}, set()
    for name, source in sources.items():
        if source.get("error"):
            errors[name] = source["error"]
            continue
        payload = source.get("data")
        try:
            if not isinstance(payload, dict):
                raise TypeError("QWeather response must be an object")
            # v1 normally uses HTTP status; reject legacy/business error envelopes as well.
            if "code" in payload and str(payload["code"]) != "200":
                raise ValueError("QWeather API returned a non-success business code")
            normalized[name] = (
                normalize_daily(payload, target_date)
                if name == "daily"
                else normalize_current(payload)
            )
            attributions.update(
                value
                for value in payload.get("metadata", {}).get("attributions", [])
                if isinstance(value, str)
            )
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            errors[name] = f"invalid_response:{type(exc).__name__}"
    is_pudong = latitude == PUDONG.latitude and longitude == PUDONG.longitude
    return {
        "provider": "QWeather",
        "role": "supplementary_grid_weather",
        "location": {
            "latitude": latitude,
            "longitude": longitude,
            "timezone": PUDONG.timezone,
            "name": PUDONG.name if is_pudong else "自定义坐标",
            "reference_station": PUDONG.icao if is_pudong else None,
            "data_type": "coordinate_grid_weather",
        },
        "target_date": target_date,
        "complete": not errors and len(normalized) == 2,
        "forecast": normalized.get("daily"),
        "current": normalized.get("current"),
        "attributions": sorted(attributions),
        "errors": errors,
        "sources": sources,
    }


def temperature_range(values):
    if not values:
        return None
    return {
        "min": min(values),
        "max": max(values),
        "mean": round(fmean(values), 6),
        "spread": round(max(values) - min(values), 6),
        "points": len(values),
    }


def summarize_points(points):
    """Compare matching periods/times only; partial coverage is always explicit."""
    forecasts = [point["forecast"] for point in points if point.get("forecast")]
    currents = [point["current"] for point in points if point.get("current")]
    periods = sorted(
        {(timestamp(row["period_start"]), timestamp(row["period_end"])) for row in forecasts}
    )
    times = sorted({timestamp(row["observed_at"]) for row in currents})
    forecasts_comparable = len(periods) == 1
    currents_comparable = len(times) == 1
    return {
        "expected_points": len(points),
        "complete_points": sum(point["complete"] for point in points),
        "forecast_points": len(forecasts),
        "current_points": len(currents),
        "forecast_periods_match": forecasts_comparable,
        "forecast_periods": [[start.isoformat(), end.isoformat()] for start, end in periods],
        "forecast_calendar_day_aligned": bool(forecasts)
        and all(row["calendar_day_aligned"] for row in forecasts),
        "forecast_max_c": temperature_range([row["temperature_max_c"] for row in forecasts])
        if forecasts_comparable
        else None,
        "forecast_min_c": temperature_range([row["temperature_min_c"] for row in forecasts])
        if forecasts_comparable
        else None,
        "current_times_match": currents_comparable,
        "current_valid_times": [value.isoformat() for value in times],
        "current_c": temperature_range([row["temperature_c"] for row in currents])
        if currents_comparable
        else None,
        "independent_models": False,
    }


def collect_airport_qweather(config: QWeatherConfig, target_date: str):
    """Five airport-area samples, ten HTTP calls, at most four in flight at a time."""
    points = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            (
                point,
                pool.submit(collect_qweather, config, target_date, point.latitude, point.longitude),
            )
            for point in PUDONG_SAMPLING_POINTS
        ]
        for point, future in futures:
            payload = future.result()
            payload["location"].update(name=point.name, reference_station=PUDONG.icao)
            points.append({"point_id": point.id, **payload})
    return {
        "schema": "weather99.qweather.airport.v1",
        "provider": "QWeather",
        "role": "supplementary_grid_weather",
        "location": PUDONG.metadata(),
        "sampling_layout": "pudong_cross_1km_v1",
        "target_date": target_date,
        "complete": all(point["complete"] for point in points),
        "summary": summarize_points(points),
        "points": points,
        "attributions": sorted({value for point in points for value in point["attributions"]}),
        "errors": {point["point_id"]: point["errors"] for point in points if point["errors"]},
    }


def airport_weather_for_date(bundle, target_date):
    """Select another day from the same seven-day responses without another HTTP request."""
    date.fromisoformat(target_date)
    points = []
    for point in bundle["points"]:
        errors = dict(point["errors"])
        source = point["sources"]["daily"]
        forecast = None
        if not source.get("error"):
            try:
                payload = source.get("data") or {}
                if "code" in payload and str(payload["code"]) != "200":
                    raise ValueError("QWeather returned a business error")
                forecast = normalize_daily(payload, target_date)
                errors.pop("daily", None)
            except (ValueError, KeyError, TypeError, AttributeError) as exc:
                errors["daily"] = f"invalid_response:{type(exc).__name__}"
        points.append(
            {
                **point,
                "forecast": forecast,
                "target_date": target_date,
                "errors": errors,
                "complete": forecast is not None and point["current"] is not None and not errors,
            }
        )
    return {
        **bundle,
        "target_date": target_date,
        "points": points,
        "summary": summarize_points(points),
        "complete": all(point["complete"] for point in points),
        "errors": {point["point_id"]: point["errors"] for point in points if point["errors"]},
    }
