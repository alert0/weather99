"""QWeather grid weather provider; kept separate from model and settlement inputs."""

import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date
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
    """Fetch current grid weather at one coordinate."""
    date.fromisoformat(target_date)
    for value, name, limit in ((latitude, "latitude", 90), (longitude, "longitude", 180)):
        finite(value, name)
        if not -limit <= value <= limit:
            raise ValueError(f"invalid {name}")
    base = f"https://{config.host}/weather/v1"
    coordinates = f"{latitude}/{longitude}"
    source = fetch(
        f"{base}/current/{coordinates}",
        {"localTime": "true", "lang": "zh"},
        headers=config.auth_headers(),
    )
    current, errors, attributions = None, {}, []
    if source.get("error"):
        errors["current"] = source["error"]
    else:
        try:
            payload = source.get("data")
            if not isinstance(payload, dict):
                raise TypeError("QWeather response must be an object")
            if "code" in payload and str(payload["code"]) != "200":
                raise ValueError("QWeather API returned a non-success business code")
            attributions = sorted(
                {
                    value
                    for value in payload.get("metadata", {}).get("attributions", [])
                    if isinstance(value, str)
                }
            )
            current = normalize_current(payload)
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            errors["current"] = f"invalid_response:{type(exc).__name__}"
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
        "complete": current is not None,
        "current": current,
        "attributions": attributions,
        "errors": errors,
        "sources": {"current": source},
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
    """Compare values only when their provider timestamps match."""
    currents = [point["current"] for point in points if point.get("current")]
    times = sorted({timestamp(row["observed_at"]) for row in currents})
    currents_comparable = len(times) == 1
    return {
        "expected_points": len(points),
        "complete_points": sum(point["complete"] for point in points),
        "current_points": len(currents),
        "current_times_match": currents_comparable,
        "current_valid_times": [value.isoformat() for value in times],
        "current_c": temperature_range([row["temperature_c"] for row in currents])
        if currents_comparable
        else None,
        "independent_models": False,
    }


def collect_daily_forecast(config: QWeatherConfig):
    """One airport-center request; retain provider intervals rather than invent a daily cutoff."""
    source = fetch(
        f"https://{config.host}/weather/v1/daily/{PUDONG.latitude}/{PUDONG.longitude}",
        {"days": 3, "localTime": "true", "lang": "zh"}, headers=config.auth_headers(),
    )
    days, errors, attributions = [], {}, []
    if source.get("error"):
        errors["daily"] = source["error"]
    else:
        try:
            payload = source["data"]
            attributions = [v for v in payload.get("metadata", {}).get("attributions", [])
                            if isinstance(v, str)]
            for row in payload["days"]:
                start, end = timestamp(row["forecastStartTime"]), timestamp(row["forecastEndTime"])
                high, low = temperature_c(row["temperatureMax"]), temperature_c(row["temperatureMin"])
                if end <= start or high < low:
                    raise ValueError("invalid forecast interval or temperature range")
                days.append({"date": start.astimezone(SHANGHAI).date().isoformat(),
                             "start_at": start.isoformat(), "end_at": end.isoformat(),
                             "max_c": high, "min_c": low,
                             "day_condition": (row.get("daytime", {}).get("condition") or {}).get("text"),
                             "night_condition": (row.get("nighttime", {}).get("condition") or {}).get("text")})
            if not days:
                raise ValueError("empty forecast")
        except (KeyError, ValueError, TypeError, AttributeError):
            days = []
            errors["daily"] = "invalid_response"
    return {"complete": bool(days) and not errors, "location": PUDONG.metadata(),
            "collected_at": source.get("fetched_at"), "days": days, "errors": errors,
            "attributions": attributions, "sources": {"daily": source}}


def collect_airport_qweather(config: QWeatherConfig, target_date: str, *, include_forecast=False):
    """Five current-weather requests, one per airport-area sample."""
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
    result = {
        "schema": "weather99.qweather.airport.v2",
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
    if include_forecast:
        result["daily_forecast"] = collect_daily_forecast(config)
        result["attributions"] = sorted(set(result["attributions"] + result["daily_forecast"]["attributions"]))
    return result
