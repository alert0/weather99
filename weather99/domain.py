"""Immutable contracts shared by forecasting, signal evaluation and persistence."""

import json
import math
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
from typing import Any

MODELS = ("ecmwf_ifs025", "gfs_global", "cma_grapes_global", "jma_seamless")
SHANGHAI = timezone(timedelta(hours=8))


def digest(value: Any) -> str:
    return sha256(
        json.dumps(value, sort_keys=True, allow_nan=False, separators=(",", ":")).encode()
    ).hexdigest()


def timestamp(value: str) -> datetime:
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("timestamp must include timezone")
    return result


def finite(value: float, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")


def model_vector(values: dict[str, float]) -> tuple[float, ...]:
    if set(values) != set(MODELS):
        raise ValueError("all four named weather models are required")
    result = tuple(values[name] for name in MODELS)
    for value in result:
        finite(value, "model temperature")
    return result


@dataclass(frozen=True)
class TrainingSample:
    target_date: str
    issued_at: str
    settled_at: str
    models_c: tuple[float, ...]
    actual_c: float
    layer: str = "24h"
    station: str = "ZSPD"

    def __post_init__(self):
        day = date.fromisoformat(self.target_date)
        issued, settled = timestamp(self.issued_at), timestamp(self.settled_at)
        issue_day = issued.astimezone(SHANGHAI).date()
        if issue_day > day or (self.layer == "24h" and issue_day == day):
            raise ValueError(
                "forecast must precede target day for 24h, or be on/before target day for intraday"
            )
        if settled.astimezone(SHANGHAI).date() <= day or settled <= issued:
            raise ValueError("settlement must be available after the target day")
        if len(self.models_c) != 4:
            raise ValueError("training needs four model temperatures")
        for value in (*self.models_c, self.actual_c):
            finite(value, "training value")
        if self.layer not in ("24h", "12h", "6h", "3h") or self.station != "ZSPD":
            raise ValueError("trainer requires a supported layer and ZSPD station")


@dataclass(frozen=True)
class ForecastEstimate:
    raw_center_c: float
    calibrated_center_c: float
    center_c: float
    sigma_c: float
    disagreement_c: float
    model_version: str
    method: str
    available_at: str

    def __post_init__(self):
        for field in (
            self.raw_center_c,
            self.calibrated_center_c,
            self.center_c,
            self.sigma_c,
            self.disagreement_c,
        ):
            finite(field, "forecast estimate")
        if self.sigma_c <= 0 or self.disagreement_c < 0:
            raise ValueError("sigma must be positive and disagreement nonnegative")
        timestamp(self.available_at)


@dataclass(frozen=True)
class Bucket:
    key: str
    lower_c: int | None
    upper_c: int | None
    bid: float | None
    ask: float | None
    market_probability: float | None
    quoted_at: str

    def __post_init__(self):
        for bound in (self.lower_c, self.upper_c):
            if bound is not None and (isinstance(bound, bool) or not isinstance(bound, int)):
                raise ValueError("bucket bounds must be integer Celsius")
        if self.lower_c is not None and self.upper_c is not None and self.lower_c > self.upper_c:
            raise ValueError("reversed bucket interval")
        if self.lower_c is None and self.upper_c is None:
            raise ValueError("bucket cannot cover every temperature")
        for value in (self.bid, self.ask, self.market_probability):
            if value is not None:
                finite(value, "bucket price/probability")
                if not 0 <= value <= 1:
                    raise ValueError("prices/probabilities must be between zero and one")
        timestamp(self.quoted_at)

    def contains(self, degree: int) -> bool:
        return (self.lower_c is None or degree >= self.lower_c) and (
            self.upper_c is None or degree <= self.upper_c
        )


@dataclass(frozen=True)
class MarketSnapshot:
    snapshot_id: str
    target_date: str
    layer: str
    as_of: str
    closes_at: str
    buckets: tuple[Bucket, ...]
    forecast: ForecastEstimate
    station: str = "ZSPD"
    observed_max_c: float | None = None

    def __post_init__(self):
        date.fromisoformat(self.target_date)
        timestamp(self.as_of)
        timestamp(self.closes_at)
        if not self.snapshot_id or self.station != "ZSPD":
            raise ValueError("snapshot id and ZSPD station are required")
        if self.observed_max_c is not None:
            finite(self.observed_max_c, "observed maximum")


@dataclass(frozen=True)
class RiskState:
    trading_date: str
    daily_realized_usd: float
    consecutive_losses: int
    equity_usd: float
    peak_equity_usd: float

    def __post_init__(self):
        date.fromisoformat(self.trading_date)
        for value in (self.daily_realized_usd, self.equity_usd, self.peak_equity_usd):
            finite(value, "risk value")
        if self.peak_equity_usd <= 0 or not 0 <= self.equity_usd <= self.peak_equity_usd:
            raise ValueError("risk state requires 0 <= equity <= positive peak equity")
        if (
            isinstance(self.consecutive_losses, bool)
            or not isinstance(self.consecutive_losses, int)
            or self.consecutive_losses < 0
        ):
            raise ValueError("consecutive losses must be a nonnegative integer")


@dataclass(frozen=True)
class Candidate:
    buckets: tuple[str, ...]
    weather_probability: float
    market_probability: float
    blended_probability: float
    cost_per_share_set: float
    reserve_per_share_set: float
    net_edge: float
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class Evaluation:
    snapshot_id: str
    config_version: str
    model_version: str
    target_date: str
    layer: str
    as_of: str
    reasons: tuple[str, ...]
    weather_mode: str | None
    market_mode: str | None
    bucket_probabilities: tuple[tuple[str, float, float], ...]
    candidates: tuple[Candidate, ...]
    selected: Candidate | None

    @property
    def eligible(self) -> bool:
        return not self.reasons and self.selected is not None

    def to_dict(self) -> dict:
        return {**asdict(self), "eligible": self.eligible}
