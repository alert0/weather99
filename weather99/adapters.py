"""External snapshot/document formats are translated here, outside the strategy core."""

import re
from datetime import UTC, datetime

from .domain import SHANGHAI, Bucket, ForecastEstimate, MarketSnapshot, digest, finite, timestamp
from .forecasting import ModelArtifact
from .nowcast import correct_forecast
from .probability import parse_bounds


def bucket_label(row: dict) -> str:
    if row.get("bucket_label"):
        return row["bucket_label"]
    question = row.get("question", "")
    # Anchored extraction avoids interpreting a day/month/year as a temperature.
    match = re.search(r"\bbe\s+(.+?)\s+on\s+", question, re.IGNORECASE)
    if not match:
        raise ValueError(f"cannot extract temperature interval from question: {question!r}")
    parse_bounds(match[1])
    return match[1]


def live_snapshot(raw: dict, model: ModelArtifact, layer: str) -> MarketSnapshot:
    if raw.get("schema") != "weather99.snapshot.v1":
        raise ValueError("unsupported capture schema")
    if raw.get("errors"):
        raise ValueError("capture contains failed sources; cannot evaluate as complete")
    if raw.get("station") != "ZSPD":
        raise ValueError("only Shanghai Pudong station is supported")
    if model.layer != layer:
        raise ValueError("model horizon does not match evaluation layer")
    as_of = raw["collected_at"]
    target = raw["forecast"]["date"]
    forecast_source = raw.get("sources", {}).get("forecast", {})
    forecast_at = forecast_source.get("fetched_at")
    if (
        not forecast_at
        or not 0 <= (timestamp(as_of) - timestamp(forecast_at)).total_seconds() <= 300
    ):
        raise ValueError("forecast capture timestamp missing, stale or in the future")
    event_source = raw.get("sources", {}).get("event", {})
    event_at = event_source.get("fetched_at")
    if not event_at or not 0 <= (timestamp(as_of) - timestamp(event_at)).total_seconds() <= 300:
        raise ValueError("market probability source timestamp missing, stale or in the future")
    event = event_source.get("data") or {}
    if event.get("slug") != raw.get("event_slug"):
        raise ValueError("event identity does not match capture")
    if event.get("closed") or not event.get("active") or not event.get("endDate"):
        raise ValueError("event must be active with an explicit close timestamp")
    buckets = []
    for row in raw["buckets"]:
        low, high = parse_bounds(bucket_label(row))
        book_time = row.get("book_timestamp")
        if book_time is None:
            raise ValueError("order book timestamp missing")
        quoted_at = datetime.fromtimestamp(float(book_time) / 1000, UTC).isoformat()
        buckets.append(
            Bucket(
                row["token_id"],
                low,
                high,
                row.get("bid"),
                row.get("ask"),
                row.get("market_probability"),
                quoted_at,
            )
        )
    observed_max = None
    if target == timestamp(as_of).astimezone(SHANGHAI).date().isoformat():
        observations = raw.get("sources", {}).get("observation", {}).get("data") or []
        valid = []
        for row in observations:
            if row.get("icaoId") != "ZSPD" or row.get("temp") is None or row.get("obsTime") is None:
                continue
            observed_at = datetime.fromtimestamp(float(row["obsTime"]), UTC)
            if observed_at.astimezone(
                SHANGHAI
            ).date().isoformat() == target and observed_at <= timestamp(as_of):
                finite(row["temp"], "METAR temperature")
                valid.append((observed_at, row["temp"]))
        if not valid or (timestamp(as_of) - max(time for time, _ in valid)).total_seconds() > 3600:
            raise ValueError("today's forecast requires a ZSPD observation no older than one hour")
        observed_max = max(temp for _, temp in valid)
        evidence = raw.get("settlement")
        if evidence is not None:
            if not evidence.get("rules_verified") or evidence.get("observed_max_c") is None:
                raise ValueError("settlement_evidence_missing")
            latest = evidence.get("latest_observed_at")
            if not latest or not 0 <= (timestamp(as_of) - timestamp(latest)).total_seconds() <= 3600:
                raise ValueError("settlement_observation_stale")
            observed_max = evidence["observed_max_c"]
    forecast, _ = correct_forecast(raw, model.predict(raw["forecast"]["models_c"]), observed_max)
    return MarketSnapshot(
        digest(raw),
        target,
        layer,
        as_of,
        event["endDate"],
        tuple(buckets),
        forecast,
        observed_max_c=observed_max,
    )


def document_snapshot(raw: dict) -> MarketSnapshot:
    signal = raw["current_signal"]
    as_of = signal["sampled_at"].replace(" ", "T") + "+08:00"
    target_date, layer = signal["date_layer"].split()
    rows = []
    for row in signal["buckets"]:
        low, high = parse_bounds(row["bucket"])
        rows.append(
            Bucket(
                row["bucket"],
                low,
                high,
                row.get("bid"),
                row.get("ask"),
                row["market_pct"] / 100,
                as_of,
            )
        )
    model = raw["model"]
    estimate = ForecastEstimate(
        model["raw_equal_weight_center_c"],
        model["bias_calibrated_center_c"],
        model["enhanced_center_c"],
        model["error_scale_c"],
        model["model_disagreement_c"],
        "document-reference-" + digest(model),
        "recorded_document_estimate_not_trained",
        model["enhanced_regression"]["updated_at"].replace(" ", "T") + "+08:00",
    )
    # This is a replay-only day-end assumption; the actual event close was not captured by FMZ.
    return MarketSnapshot(
        "document-" + digest(raw),
        target_date,
        layer,
        as_of,
        target_date + "T23:59:59+08:00",
        tuple(rows),
        estimate,
    )
