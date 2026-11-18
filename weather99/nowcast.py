"""Replayable intraday residual correction; research parameters, not fitted skill."""

import math
from dataclasses import replace
from datetime import datetime
from statistics import fmean

from .domain import MODELS, SHANGHAI, digest, timestamp
from .settlement import metar_evidence

PARAMETERS = {"method": "intraday_residual_v1", "metar_weight": .75,
              "qweather_weight": .25, "gain": .5, "limit_c": 2., "max_age_seconds": 3600}


def valid_number(value):
    return type(value) in (int, float) and math.isfinite(value)


def hourly_temperature(raw, observed_at):
    """Interpolate all four models at the observation's valid time, in Shanghai time."""
    hourly = (raw.get("sources", {}).get("forecast", {}).get("data") or {}).get("hourly", {})
    moment = timestamp(observed_at)
    samples = []
    for index, text in enumerate(hourly.get("time", [])):
        try:
            at = datetime.fromisoformat(text)
            if at.tzinfo is None:
                if raw.get("timezone") != "Asia/Shanghai":
                    return None
                at = at.replace(tzinfo=SHANGHAI)
            values = [hourly.get(f"temperature_2m_{model}", [])[index] for model in MODELS]
            if all(valid_number(value) for value in values):
                samples.append((at, fmean(values)))
        except (ValueError, TypeError, IndexError):
            continue
    samples.sort()
    for at, value in samples:
        if at == moment:
            return value
    for (left, low), (right, high) in zip(samples, samples[1:]):
        span = (right - left).total_seconds()
        if left < moment < right and 0 < span <= 3600:
            return low + (high - low) * (moment - left).total_seconds() / span
    return None


def realtime_sources(raw, as_of=None):
    cutoff = timestamp(as_of or raw["collected_at"])
    day = cutoff.astimezone(SHANGHAI).date().isoformat()
    source = raw.get("sources", {}).get("observation", {})
    evidence = metar_evidence(source.get("data"), day, cutoff.isoformat())
    latest = evidence["observations"][-1] if evidence["observations"] else {}
    metar = {"source": "METAR · ZSPD", **latest,
             "observed_max_c": evidence["observed_max_c"], "status": "missing"}
    if source.get("error"):
        metar["status"] = "source_error"
    bundle = raw.get("auxiliary_weather", {}).get("qweather") or {}
    summary = bundle.get("summary") or {}
    current = summary.get("current_c") or {}
    times = summary.get("current_valid_times") or []
    qweather = {"source": "和风 · 五点网格", "status": "missing"}
    if bundle.get("complete") and summary.get("current_times_match") and len(times) == 1:
        qweather.update(temperature_c=current.get("mean"), observed_at=times[0],
                        min_c=current.get("min"), max_c=current.get("max"))
    elif bundle:
        qweather["status"] = "incomplete"
    for row in (metar, qweather):
        if not valid_number(row.get("temperature_c")) or not row.get("observed_at"):
            continue
        try:
            age = (cutoff - timestamp(row["observed_at"])).total_seconds()
        except (ValueError, TypeError):
            row["status"] = "invalid_time"
            continue
        row["age_seconds"] = age
        if row["status"] == "source_error":
            continue
        row["status"] = "fresh" if 0 <= age <= PARAMETERS["max_age_seconds"] else "stale" if age > 0 else "future"
        matched = hourly_temperature(raw, row["observed_at"])
        row["model_current_c"] = matched
        row["residual_c"] = row["temperature_c"] - matched if matched is not None else None
    return {"metar": metar, "qweather": qweather}


def correct_forecast(raw, forecast, observed_max=None):
    sources = realtime_sources(raw)
    detail = {"parameters": PARAMETERS, "sources": sources, "status": "not_target_day",
              "before_c": forecast.center_c, "after_c": forecast.center_c, "adjustment_c": 0.}
    today = timestamp(raw["collected_at"]).astimezone(SHANGHAI).date().isoformat()
    if raw["forecast"]["date"] != today:
        return forecast, detail
    eligible = [(PARAMETERS[f"{key}_weight"], row["residual_c"])
                for key, row in sources.items()
                if row["status"] == "fresh" and row.get("residual_c") is not None
                and timestamp(row["observed_at"]).astimezone(SHANGHAI).date().isoformat() == today]
    if not eligible:
        detail["status"] = "no_aligned_observations"
        return forecast, detail
    shift = max(-PARAMETERS["limit_c"], min(PARAMETERS["limit_c"],
                PARAMETERS["gain"] * sum(weight * residual for weight, residual in eligible)))
    center = forecast.center_c + shift
    if observed_max is not None:
        center = max(center, observed_max)
    detail.update(status="applied", after_c=center, adjustment_c=center - forecast.center_c,
                  residual_shift_c=shift)
    return replace(forecast, center_c=center, method=forecast.method + "+intraday_residual_v1",
                   model_version=digest({"base": forecast.model_version, **PARAMETERS})), detail
