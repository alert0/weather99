"""Versioned operational parameters; historical account/performance is never loaded."""

import json
from dataclasses import dataclass
from pathlib import Path

from .domain import digest, finite

DEFAULT_CONFIG = Path(__file__).resolve().parents[1] / "config/strategy.json"
LAYERS = ("24h", "12h", "6h", "3h")


@dataclass(frozen=True)
class StrategyConfig:
    version: str
    max_buckets: int
    probability_threshold: tuple[float, ...]
    edge_threshold: tuple[float, ...]
    must_include_market_mode: bool
    mode_gap_max_c: float
    extreme_disagreement_c: float
    market_weight: float
    confirmations: int
    fee_reserve: float
    slippage_reserve: float
    daily_loss_limit_usd: float
    consecutive_loss_limit: int
    drawdown_halt: float
    ask_min: float
    ask_max: float
    max_spread: float
    quote_max_age_seconds: int
    confirmation_max_gap_seconds: int
    max_market_buckets: int

    def thresholds(self, layer: str) -> tuple[float, float]:
        if layer not in LAYERS:
            raise ValueError(f"unsupported layer: {layer}")
        index = LAYERS.index(layer)
        return self.probability_threshold[index], self.edge_threshold[index]


def load_config(path: Path = DEFAULT_CONFIG) -> tuple[StrategyConfig, dict]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if raw["schema"] != "weather99.strategy.v1":
        raise ValueError("unsupported strategy config schema")
    entry, risk, research = raw["entry"], raw["risk"], raw["research_assumptions"]
    expected = {
        "probability_family": "normal_integer_rounding",
        "market_probability": "normalized_provided_yes_prices",
        "net_edge": "weather_probability_minus_ask_cost_minus_fixed_reserves",
        "disagreement": "maximum_minus_minimum",
    }
    if any(research.get(key) != value for key, value in expected.items()):
        raise ValueError("unsupported research formula")
    for section in (entry, risk, research, raw["training"]):
        for key, value in section.items():
            if isinstance(value, (float, int)) and not isinstance(value, bool):
                finite(value, key)
    probabilities = tuple(entry["probability_threshold"][layer] for layer in LAYERS)
    edges = tuple(entry["edge_threshold"][layer] for layer in LAYERS)
    for value in (
        *probabilities,
        *edges,
        entry["fee_reserve"],
        entry["slippage_reserve"],
        research["market_weight"],
        entry["market_weight_cap"],
        risk["drawdown_halt"],
    ):
        finite(value, "probability/weight/threshold")
        if not 0 <= value <= 1:
            raise ValueError("probabilities, weights, reserves and thresholds must be in [0, 1]")
    if research["market_weight"] > entry["market_weight_cap"]:
        raise ValueError("market blend weight exceeds documented cap")
    for section, keys in (
        (entry, ("max_buckets", "confirmations")),
        (risk, ("consecutive_loss_limit",)),
        (research, ("quote_max_age_seconds", "confirmation_max_gap_seconds", "max_market_buckets")),
    ):
        for key in keys:
            if type(section[key]) is not int or section[key] < 1:
                raise ValueError(f"{key} must be a positive integer")
    if not 1 <= entry["max_buckets"] <= 3 or not 1 <= research["max_market_buckets"] <= 64:
        raise ValueError("candidate enumeration limits exceeded")
    if (
        not 0 <= research["ask_min"] <= research["ask_max"] <= 1
        or not 0 <= research["max_spread"] <= 1
    ):
        raise ValueError("invalid quote limits")
    if risk["daily_loss_limit_usd"] >= 0 or not 0 < risk["drawdown_halt"] <= 1:
        raise ValueError("invalid loss/drawdown limits")
    if entry["mode_gap_max_c"] < 0 or entry["extreme_disagreement_c"] <= 0:
        raise ValueError("invalid temperature limits")
    config = StrategyConfig(
        digest(raw),
        entry["max_buckets"],
        probabilities,
        edges,
        entry["must_include_market_mode"],
        entry["mode_gap_max_c"],
        entry["extreme_disagreement_c"],
        research["market_weight"],
        entry["confirmations"],
        entry["fee_reserve"],
        entry["slippage_reserve"],
        risk["daily_loss_limit_usd"],
        risk["consecutive_loss_limit"],
        risk["drawdown_halt"],
        research["ask_min"],
        research["ask_max"],
        research["max_spread"],
        research["quote_max_age_seconds"],
        research["confirmation_max_gap_seconds"],
        research["max_market_buckets"],
    )
    return config, raw
