"""Rolling bias calibration and ridge regression with point-in-time training cuts."""

import json
import math
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from pathlib import Path
from statistics import fmean

from .domain import (
    MODELS,
    ForecastEstimate,
    TrainingSample,
    digest,
    finite,
    model_vector,
    timestamp,
)


@dataclass(frozen=True)
class TrainingOptions:
    rolling_days: int = 60
    ridge_days: int = 540
    min_samples: int = 60
    ridge_alpha: float = 3.0
    sigma_intercept: float = 0.4994
    sigma_disagreement_coefficient: float = 0.072
    sigma_floor_c: float = 0.1

    def __post_init__(self):
        for value in (self.rolling_days, self.ridge_days, self.min_samples):
            if type(value) is not int or value < 1:
                raise ValueError("training windows and minimum samples must be positive integers")
        if not 5 <= self.min_samples <= min(self.rolling_days, self.ridge_days):
            raise ValueError("minimum samples must fit both windows and be at least five")
        for value in (
            self.ridge_alpha,
            self.sigma_intercept,
            self.sigma_disagreement_coefficient,
            self.sigma_floor_c,
        ):
            finite(value, "training option")
        if (
            self.ridge_alpha <= 0
            or self.sigma_intercept <= 0
            or self.sigma_disagreement_coefficient < 0
            or self.sigma_floor_c <= 0
        ):
            raise ValueError("invalid regularization or error-scale parameters")


@dataclass(frozen=True)
class ModelArtifact:
    version: str
    trained_at: str
    layer: str
    training_digest: str
    train_start: str
    train_end: str
    samples: int
    rolling_samples: int
    raw_bias_c: float
    per_model_bias_c: tuple[float, ...]
    calibrated_rmse_in_sample_c: float
    ridge_coefficients: tuple[float, ...]
    ridge_intercept: float
    options: TrainingOptions

    def payload(self) -> dict:
        data = asdict(self)
        data.pop("version")
        return data

    def predict(self, models: dict[str, float]) -> ForecastEstimate:
        vector = model_vector(models)
        raw = fmean(vector)
        disagreement = max(vector) - min(vector)
        center = self.ridge_intercept + sum(a * b for a, b in zip(self.ridge_coefficients, vector))
        sigma = max(
            self.options.sigma_floor_c,
            math.sqrt(
                self.options.sigma_intercept
                + self.options.sigma_disagreement_coefficient * disagreement**2
            ),
        )
        return ForecastEstimate(
            raw,
            raw - self.raw_bias_c,
            center,
            sigma,
            disagreement,
            self.version,
            "ridge_four_model_v1",
            self.trained_at,
        )

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8") as handle:
            json.dump(asdict(self), handle, ensure_ascii=False, indent=2, allow_nan=False)

    @classmethod
    def load(cls, path: Path) -> "ModelArtifact":
        data = json.loads(path.read_text(encoding="utf-8"))
        data["options"] = TrainingOptions(**data["options"])
        for key in ("ridge_coefficients", "per_model_bias_c"):
            data[key] = tuple(data[key])
        artifact = cls(**data)
        if digest(artifact.payload()) != artifact.version:
            raise ValueError("model artifact checksum mismatch")
        if len(artifact.ridge_coefficients) != 4 or len(artifact.per_model_bias_c) != 4:
            raise ValueError("model artifact has invalid feature dimensions")
        timestamp(artifact.trained_at)
        return artifact


def load_samples(path: Path) -> list[TrainingSample]:
    rows = json.loads(path.read_text(encoding="utf-8"))
    result = []
    for row in rows:
        data = dict(row)
        data["models_c"] = model_vector(data["models_c"])
        result.append(TrainingSample(**data))
    return result


def fit(
    samples: list[TrainingSample],
    as_of: str,
    options: TrainingOptions = TrainingOptions(),
    layer: str = "24h",
) -> ModelArtifact:
    import numpy as np

    cutoff = timestamp(as_of)
    if layer not in ("24h", "12h", "6h", "3h"):
        raise ValueError("unsupported training layer")
    from .domain import SHANGHAI

    cutoff_day = cutoff.astimezone(SHANGHAI).date()
    # No future labels, same-day targets, duplicate dates, or revised forecasts after issuance.
    eligible = [
        sample
        for sample in samples
        if sample.layer == layer
        and date.fromisoformat(sample.target_date) < cutoff_day
        and timestamp(sample.settled_at) <= cutoff
        and timestamp(sample.issued_at) < cutoff
        and date.fromisoformat(sample.target_date)
        >= cutoff_day - timedelta(days=options.ridge_days)
    ]
    eligible.sort(key=lambda sample: sample.target_date)
    if len({sample.target_date for sample in eligible}) != len(eligible):
        raise ValueError("duplicate target dates in training; select one forecast vintage per day")
    rolling = [
        sample
        for sample in eligible
        if date.fromisoformat(sample.target_date)
        >= cutoff_day - timedelta(days=options.rolling_days)
    ]
    if min(len(eligible), len(rolling)) < options.min_samples:
        raise ValueError(
            f"insufficient settled history: ridge={len(eligible)}, rolling={len(rolling)}, required={options.min_samples}"
        )
    x = np.array([sample.models_c for sample in eligible], dtype=float)
    y = np.array([sample.actual_c for sample in eligible], dtype=float)
    x_mean, y_mean = x.mean(axis=0), float(y.mean())
    centered_x = x - x_mean
    # Ridge penalizes feature coefficients only; intercept remains unpenalized.
    coefficients = np.linalg.solve(
        centered_x.T @ centered_x + options.ridge_alpha * np.eye(4), centered_x.T @ (y - y_mean)
    )
    intercept = y_mean - float(x_mean @ coefficients)
    biases = tuple(
        fmean(sample.models_c[index] - sample.actual_c for sample in rolling) for index in range(4)
    )
    bias = fmean(biases)
    rmse = math.sqrt(
        fmean((fmean(sample.models_c) - bias - sample.actual_c) ** 2 for sample in rolling)
    )
    payload = {
        "trained_at": as_of,
        "layer": layer,
        "training_digest": digest([asdict(s) for s in eligible]),
        "train_start": eligible[0].target_date,
        "train_end": eligible[-1].target_date,
        "samples": len(eligible),
        "rolling_samples": len(rolling),
        "raw_bias_c": bias,
        "per_model_bias_c": biases,
        "calibrated_rmse_in_sample_c": rmse,
        "ridge_coefficients": tuple(float(value) for value in coefficients),
        "ridge_intercept": intercept,
        "options": options,
    }
    artifact = ModelArtifact(version="", **payload)
    return ModelArtifact(version=digest(artifact.payload()), **payload)


def walk_forward(
    samples: list[TrainingSample],
    options: TrainingOptions,
    validation_days: int = 180,
    layer: str = "24h",
) -> dict:
    if validation_days < 1:
        raise ValueError("validation days must be positive")
    ordered = sorted(
        (s for s in samples if s.layer == layer), key=lambda sample: sample.target_date
    )
    if len({s.target_date for s in ordered}) != len(ordered):
        raise ValueError("duplicate validation target dates")
    rows, skipped = [], 0
    for sample in ordered[-validation_days:]:
        try:
            model = fit(ordered, sample.issued_at, options, layer)
        except ValueError as exc:
            if not str(exc).startswith("insufficient settled history"):
                raise
            skipped += 1
            continue
        forecast = model.predict(dict(zip(MODELS, sample.models_c)))
        rows.append(
            {
                "date": sample.target_date,
                "model_version": model.version,
                "trained_through": model.train_end,
                "prediction_c": forecast.center_c,
                "actual_c": sample.actual_c,
                "error_c": forecast.center_c - sample.actual_c,
                "raw_error_c": forecast.raw_center_c - sample.actual_c,
            }
        )
    return {
        "samples": len(rows),
        "skipped_insufficient_history": skipped,
        "ridge_mae_c": fmean(abs(row["error_c"]) for row in rows) if rows else None,
        "raw_mae_c": fmean(abs(row["raw_error_c"]) for row in rows) if rows else None,
        "rows": rows,
    }
