"""Auditable hourly Tmax shadow: NWP curves + ZSPD METAR + QWeather grid.

No trading permissions or weather bucket probabilities are produced here.
The correction gains are hypotheses pending timestamped forecast-vintage validation.
All calculations use the immutable collected snapshot (no network or clock reads).
"""

from __future__ import annotations

import math
from datetime import datetime
from statistics import fmean

from .domain import MODELS, SHANGHAI, ForecastEstimate, digest, timestamp
from .nowcast import realtime_sources

POLICY = {
    "method": "hourly_tmax_shadow_v1",
    "metar_weight": 0.75,
    "qweather_weight": 0.25,
    "gain": 0.5,
    "shift_cap_c": 2.0,
    "half_life_hours": 3.0,
    "max_grid_residual_gap_c": 2.0,
    "max_observation_time_gap_seconds": 1800,
}


def _finite(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _model_hourly_rows(raw: dict, target: str, as_of: datetime):
    """Return complete 4-model target-day rows and count missing future hours."""
    hourly = (raw.get("sources", {}).get("forecast", {}).get("data") or {}).get("hourly") or {}
    if not isinstance(hourly, dict):
        return [], 0
    rows, missing_future = [], 0
    for index, instant in enumerate(hourly.get("time") or []):
        moment = None
        try:
            moment = datetime.fromisoformat(str(instant))
            if moment.tzinfo is None:
                if raw.get("timezone") != "Asia/Shanghai":
                    return [], 0
                moment = moment.replace(tzinfo=SHANGHAI)
            if moment.astimezone(SHANGHAI).date().isoformat() != target:
                continue
            values = {}
            for model in MODELS:
                value = hourly[f"temperature_2m_{model}"][index]
                if not _finite(value):
                    raise ValueError("invalid temperature")
                values[model] = float(value)
            rows.append((moment, values))
        except (ValueError, KeyError, IndexError, TypeError, OverflowError):
            if moment is not None and moment > as_of and moment.astimezone(SHANGHAI).date().isoformat() == target:
                missing_future += 1
            continue
    rows.sort(key=lambda item: item[0])
    return rows, missing_future


def hourly_tmax_shadow(
    raw: dict, baseline: ForecastEstimate, observed_high_c: float | None
) -> dict:
    """Report a corrected future-hour curve and tentative final Tmax for today.

    The baseline MUST be computed before the existing daily nowcast correction.
    The observed_high_c argument MUST be the verified station-rule observation
    obtained by live_snapshot, never QWeather or a daily gridded forecast.
    """
    report = {
        "schema": "weather99.hourly_shadow.v1",
        "policy": dict(POLICY),
        "policy_version": digest(POLICY),
        "base_model_version": baseline.model_version,
        "trade_eligible": False,
        "status": "unavailable",
        "hours": [],
    }
    as_of = timestamp(raw["collected_at"])
    today = as_of.astimezone(SHANGHAI).date().isoformat()
    if (raw.get("forecast") or {}).get("date") != today:
        report["status"] = "not_target_day"
        return report
    if not _finite(observed_high_c):
        report["status"] = "verified_station_high_missing"
        return report

    evidence = realtime_sources(raw)
    station = evidence["metar"]
    grid = evidence["qweather"]
    report["source_health"] = {
        name: {
            "status": row.get("status"),
            "observed_at": row.get("observed_at"),
            "residual_c": row.get("residual_c"),
        }
        for name, row in evidence.items()
    }
    if (station.get("status") != "fresh" or not _finite(station.get("residual_c"))
            or timestamp(station["observed_at"]).astimezone(SHANGHAI).date().isoformat() != today):
        report["status"] = "metar_unavailable_or_unaligned"
        return report
    if observed_high_c + 1e-9 < station["temperature_c"]:
        report["status"] = "station_high_inconsistent"
        return report

    hourly_rows, missing = _model_hourly_rows(raw, today, as_of)
    if missing:
        report["status"] = "future_hourly_model_missing"
        report["missing_future_hours"] = missing
        return report
    future = [(moment, values) for moment, values in hourly_rows if moment > as_of]
    if not hourly_rows or not future:
        report["status"] = "future_hourly_path_missing"
        return report

    full_daily_peak = fmean(max(values[model] for _, values in hourly_rows) for model in MODELS)
    daily_alignment = baseline.center_c - full_daily_peak
    metar_residual = float(station["residual_c"])
    grid_ok = (grid.get("status") == "fresh"
               and _finite(grid.get("residual_c"))
               and timestamp(grid["observed_at"]).astimezone(SHANGHAI).date().isoformat() == today
               and abs((timestamp(grid["observed_at"]) - timestamp(station["observed_at"])).total_seconds())
               <= POLICY["max_observation_time_gap_seconds"]
               and abs(grid["residual_c"] - metar_residual) <= POLICY["max_grid_residual_gap_c"])
    residual = (
        POLICY["metar_weight"] * metar_residual + POLICY["qweather_weight"] * grid["residual_c"]
        if grid_ok else metar_residual
    )
    initial_shift = max(-POLICY["shift_cap_c"], min(POLICY["shift_cap_c"], POLICY["gain"] * residual))
    forecast_paths = {model: [] for model in MODELS}
    report_hours = []
    for moment, values in future:
        hours_ahead = (moment - as_of).total_seconds() / 3600
        shift = initial_shift * 2 ** (-hours_ahead / POLICY["half_life_hours"])
        corrected = {model: value + daily_alignment + shift for model, value in values.items()}
        for model, value in corrected.items():
            forecast_paths[model].append(value)
        report_hours.append({
            "valid_at": moment.isoformat(),
            "model_mean_c": round(fmean(values.values()), 4),
            "corrected_mean_c": round(fmean(corrected.values()), 4),
            "shift_c": round(shift, 4),
            "models_c": values,
            "corrected_models_c": {name: round(value, 4) for name, value in corrected.items()},
        })
    remaining_peak = fmean(max(values) for values in forecast_paths.values())
    report.update(
        status="shadow_ready",
        qweather_used=bool(grid_ok),
        combined_residual_c=round(residual, 4),
        metar_residual_c=round(metar_residual, 4),
        hourly_to_daily_alignment_c=round(daily_alignment, 4),
        baseline_center_c=baseline.center_c,
        observed_high_c=float(observed_high_c),
        predicted_remaining_peak_c=round(remaining_peak, 4),
        predicted_final_high_c=round(max(float(observed_high_c), remaining_peak), 4),
        hours=report_hours,
    )
    return report
