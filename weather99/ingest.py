"""Public, read-only data collection; raw responses are retained for replay."""

import ast
import json
import math
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import uuid4

from .domain import MODELS
from .http import fetch
from .locations import PUDONG
from .qweather import QWeatherConfig, collect_airport_qweather
from .settlement import collect_settlement

ROOT = Path(__file__).resolve().parents[1]
MONTHS = [
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
]


def city_metadata():
    """Read the pinned upstream registry as data, without importing its application."""
    path = ROOT / "vendor/PolyWeather/src/data_collection/city_registry.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "CITY_REGISTRY" for target in node.targets
        ):
            return ast.literal_eval(node.value)["shanghai"]
    raise ValueError("PolyWeather CITY_REGISTRY not found")


def forecast_summary(payload, target_date):
    daily = payload.get("daily", {})
    dates = daily.get("time", [])
    values = {}
    if target_date in dates:
        index = dates.index(target_date)
        for model in MODELS:
            series = daily.get(f"temperature_2m_max_{model}", [])
            if index < len(series):
                value = series[index]
                if isinstance(value, (int, float)) and math.isfinite(value):
                    values[model] = value
    return {
        "date": target_date,
        "models_c": values,
        "all_four_models": len(values) == len(MODELS),
        "raw_equal_weight_center_c": sum(values.values()) / 4 if len(values) == 4 else None,
    }


def json_array(value):
    return json.loads(value) if isinstance(value, str) else value or []


def yes_token(market):
    outcomes = json_array(market.get("outcomes"))
    tokens = json_array(market.get("clobTokenIds"))
    for index, outcome in enumerate(outcomes):
        if str(outcome).lower() == "yes" and index < len(tokens):
            return str(tokens[index])
    return None


def best_prices(book):
    def prices(side):
        return [
            float(level["price"])
            for level in book.get(side, [])
            if float(level.get("size", 0)) > 0 and 0 <= float(level["price"]) <= 1
        ]

    bids, asks = prices("bids"), prices("asks")
    bid, ask = max(bids, default=None), min(asks, default=None)
    return {
        "bid": bid,
        "ask": ask,
        "spread": ask - bid if bid is not None and ask is not None else None,
        "two_sided": bid is not None and ask is not None and bid <= ask,
    }


def collect(target_date, output, qweather: QWeatherConfig | None = None, *, auxiliary_weather=None):
    if qweather is not None and auxiliary_weather is not None:
        raise ValueError("provide either live QWeather configuration or a collected weather bundle")
    city = city_metadata()
    if city["icao"] != PUDONG.icao:
        raise ValueError("Shanghai research requires Pudong Airport ZSPD")
    # Coordinates and four-model requirements come from the local FMZ research spec.
    config = json.loads((ROOT / "doc/strategy_680665_config.json").read_text(encoding="utf-8"))
    weather = config["data_sources"]["weather"]
    day = date.fromisoformat(target_date)
    slug = f"highest-temperature-in-shanghai-on-{MONTHS[day.month - 1]}-{day.day}-{day.year}"
    jobs = {
        "forecast": (
            weather["endpoint"],
            {
                "latitude": PUDONG.latitude,
                "longitude": PUDONG.longitude,
                "daily": "temperature_2m_max",
                "hourly": ",".join(weather["hourly"]),
                "models": ",".join(MODELS),
                "timezone": weather["timezone"],
                "forecast_days": 3,
            },
        ),
        "observation": (
            "https://aviationweather.gov/api/data/metar",
            {"ids": PUDONG.icao, "format": "json", "hours": 24},
        ),
        "event": (f"https://gamma-api.polymarket.com/events/slug/{slug}", None),
    }
    with ThreadPoolExecutor(max_workers=4) as pool:
        qweather_future = (
            pool.submit(collect_airport_qweather, qweather, target_date) if qweather else None
        )
        futures = {name: pool.submit(fetch, url, params) for name, (url, params) in jobs.items()}
        sources = {name: future.result() for name, future in futures.items()}
        event = sources["event"].get("data") or {}
        settlement_future = pool.submit(collect_settlement, event, target_date)
        markets = event.get("markets", []) if isinstance(event, dict) else []
        pending = []
        for market in markets:
            token = yes_token(market)
            if token and market.get("active") and not market.get("closed"):
                pending.append(
                    (
                        market,
                        token,
                        pool.submit(fetch, "https://clob.polymarket.com/book", {"token_id": token}),
                    )
                )
        buckets = []
        for market, token, future in pending:
            response = future.result()
            sources[f"book:{token}"] = response
            buckets.append(
                {
                    "question": market.get("question"),
                    "token_id": token,
                    "bucket_label": market.get("groupItemTitle"),
                    "market_probability": yes_probability(market),
                    "condition_id": market.get("conditionId"),
                    "book_timestamp": (response.get("data") or {}).get("timestamp"),
                    **best_prices(response.get("data") or {}),
                }
            )
        settlement, settlement_response = settlement_future.result()
        if settlement_response is not None:
            sources["settlement"] = settlement_response
    forecast = forecast_summary(sources["forecast"].get("data") or {}, target_date)
    errors = {name: source["error"] for name, source in sources.items() if "error" in source}
    result = {
        "schema": "weather99.snapshot.v1",
        "collected_at": datetime.now(UTC).isoformat(),
        "city": "shanghai",
        "station": city["icao"],
        "location": PUDONG.metadata(),
        "timezone": weather["timezone"],
        "event_slug": slug,
        "forecast": forecast,
        "buckets": buckets,
        "settlement": settlement,
        "resolution_source": event.get("resolutionSource") if isinstance(event, dict) else None,
        "errors": errors,
        "sources": sources,
    }
    if qweather_future:
        result["auxiliary_weather"] = {"qweather": qweather_future.result()}
    elif auxiliary_weather is not None:
        result["auxiliary_weather"] = auxiliary_weather
    # A complete capture is only data readiness; it does not authorize or imply a trade.
    result["data_ready"] = (
        forecast["all_four_models"]
        and bool(buckets)
        and not errors
        and any(bucket["two_sided"] for bucket in buckets)
        and bool(sources["observation"].get("data"))
    )
    output.mkdir(parents=True, exist_ok=True)
    path = output / f"{datetime.now(UTC):%Y%m%dT%H%M%S}_{uuid4().hex}.json"
    with path.open("x", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)
    return path, result


def yes_probability(market):
    outcomes = json_array(market.get("outcomes"))
    prices = json_array(market.get("outcomePrices"))
    for index, outcome in enumerate(outcomes):
        if str(outcome).lower() == "yes" and index < len(prices):
            value = float(prices[index])
            return value if math.isfinite(value) and 0 <= value <= 1 else None
    return None
