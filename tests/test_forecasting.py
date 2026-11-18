import json
import unittest
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from weather99.domain import MODELS, TrainingSample
from weather99.forecasting import ModelArtifact, TrainingOptions, fit, walk_forward


def synthetic_history(days=85, layer="24h"):
    """Synthetic data is confined to tests; it is not claimed to be market history."""
    start = date(2026, 7, 1)
    rows = []
    for index in range(days):
        day = start + timedelta(days=index)
        actual = 20 + index * 0.05 + (index % 7) * 0.1
        models = tuple(actual - 1 + offset for offset in (-0.3, 0.1, 0.4, -0.2))
        rows.append(
            TrainingSample(
                day.isoformat(),
                f"{day - timedelta(days=1)}T08:00:00+08:00",
                f"{day + timedelta(days=1)}T00:00:00+08:00",
                models,
                actual,
                layer=layer,
            )
        )
    return rows


class ForecastingTests(unittest.TestCase):
    def setUp(self):
        self.samples = synthetic_history()
        self.cutoff = "2026-09-24T08:00:00+08:00"

    def test_bias_sign_ridge_and_error_formula(self):
        artifact = fit(self.samples, self.cutoff)
        forecast = artifact.predict(dict(zip(MODELS, self.samples[-1].models_c)))
        self.assertAlmostEqual(artifact.raw_bias_c, -1)
        self.assertAlmostEqual(forecast.calibrated_center_c, self.samples[-1].actual_c)
        self.assertLess(abs(forecast.center_c - self.samples[-1].actual_c), 0.1)
        self.assertAlmostEqual(forecast.sigma_c**2, 0.4994 + 0.072 * 0.7**2)
        self.assertEqual(artifact.rolling_samples, 60)

    def test_future_or_unsettled_labels_cannot_change_model(self):
        baseline = fit(self.samples, self.cutoff)
        future = synthetic_history(95)[-1]
        delayed = replace(self.samples[0], settled_at="2026-12-01T00:00:00+08:00")
        with_future = fit(self.samples + [replace(future, actual_c=999)], self.cutoff)
        self.assertEqual(baseline.version, with_future.version)
        without_delayed = fit(self.samples[1:], self.cutoff)
        with_delayed = fit([delayed] + self.samples[1:], self.cutoff)
        self.assertEqual(without_delayed.version, with_delayed.version)

    def test_duplicate_days_and_incomplete_models_rejected(self):
        with self.assertRaisesRegex(ValueError, "duplicate"):
            fit(self.samples + [self.samples[-1]], self.cutoff)
        with self.assertRaisesRegex(ValueError, "four"):
            replace(self.samples[0], models_c=(1, 2, 3))
        with self.assertRaisesRegex(ValueError, "finite"):
            replace(self.samples[0], actual_c=float("nan"))
        with self.assertRaisesRegex(ValueError, "insufficient"):
            fit(self.samples[:5], self.cutoff)

    def test_artifact_checksum_and_immutable_save(self):
        artifact = fit(self.samples, self.cutoff)
        with TemporaryDirectory() as directory:
            path = Path(directory) / "model.json"
            artifact.save(path)
            self.assertEqual(ModelArtifact.load(path), artifact)
            with self.assertRaises(FileExistsError):
                artifact.save(path)
            data = json.loads(path.read_text(encoding="utf-8"))
            data["ridge_intercept"] += 1
            path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "checksum"):
                ModelArtifact.load(path)

    def test_walk_forward_trains_strictly_before_issuance(self):
        result = walk_forward(self.samples, TrainingOptions(), validation_days=10)
        self.assertEqual(result["samples"], 10)
        for row in result["rows"]:
            self.assertLess(row["trained_through"], row["date"])
        changed = [*self.samples[:-1], replace(self.samples[-1], actual_c=999)]
        other = walk_forward(changed, TrainingOptions(), validation_days=10)
        self.assertEqual(result["rows"][-1]["prediction_c"], other["rows"][-1]["prediction_c"])

    def test_horizon_training_does_not_mix_labels(self):
        intraday = [replace(s, layer="3h") for s in self.samples]
        artifact = fit(self.samples + intraday, self.cutoff, layer="3h")
        self.assertEqual(artifact.layer, "3h")
        self.assertEqual(artifact.samples, len(self.samples))

    def test_forecast_issued_after_target_day_is_invalid(self):
        with self.assertRaisesRegex(ValueError, "forecast must"):
            replace(self.samples[0], issued_at="2026-07-02T00:00:00+08:00")


if __name__ == "__main__":
    unittest.main()
