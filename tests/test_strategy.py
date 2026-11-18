import json
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from weather99.adapters import document_snapshot
from weather99.config import load_config
from weather99.domain import Bucket, ForecastEstimate, MarketSnapshot, RiskState
from weather99.probability import parse_bounds, partition, probability
from weather99.strategy import evaluate


def scenario():
    as_of = "2026-11-16T12:00:00+08:00"
    forecast = ForecastEstimate(18, 18, 18, 0.4, 0.5, "test-model", "test", as_of)
    buckets = (
        Bucket("low", None, 17, 0.14, 0.15, 0.15, as_of),
        Bucket("18", 18, 18, 0.24, 0.25, 0.70, as_of),
        Bucket("high", 19, None, 0.14, 0.15, 0.15, as_of),
    )
    snapshot = MarketSnapshot(
        "test-snapshot", "2026-11-17", "24h", as_of, "2026-11-18T00:00:00+08:00", buckets, forecast
    )
    risk = RiskState("2026-11-16", 0, 0, 100, 100)
    return snapshot, risk


class ProbabilityTests(unittest.TestCase):
    def test_integer_partition_probability_conserves_mass_with_tails(self):
        snapshot, _ = scenario()
        self.assertAlmostEqual(sum(probability(b, snapshot.forecast) for b in snapshot.buckets), 1)
        self.assertAlmostEqual(
            probability(snapshot.buckets[0], snapshot.forecast),
            probability(snapshot.buckets[2], snapshot.forecast),
        )

    def test_observed_max_excludes_lower_outcomes_and_renormalizes(self):
        snapshot, _ = scenario()
        values = [probability(b, snapshot.forecast, 18) for b in snapshot.buckets]
        self.assertEqual(values[0], 0)
        self.assertAlmostEqual(sum(values), 1)

    def test_celsius_labels_and_unsupported_units(self):
        self.assertEqual(parse_bounds("13°C or below"), (None, 13))
        self.assertEqual(parse_bounds("23°C or higher"), (23, None))
        self.assertEqual(parse_bounds("-3--1°C"), (-3, -1))
        for label in ("68°F", "2026-11-17", "19", "5-2°C"):
            with self.assertRaises(ValueError):
                parse_bounds(label)

    def test_missing_tails_and_overlap_are_rejected(self):
        snapshot, _ = scenario()
        with self.assertRaisesRegex(ValueError, "tail"):
            partition(snapshot.buckets[1:])
        with self.assertRaisesRegex(ValueError, "overlap"):
            partition(
                (snapshot.buckets[0], replace(snapshot.buckets[1], lower_c=17), snapshot.buckets[2])
            )


class StrategyTests(unittest.TestCase):
    def setUp(self):
        self.config, self.raw_config = load_config()
        self.snapshot, self.risk = scenario()

    def test_pure_evaluation_and_equal_share_portfolio_cost(self):
        result = evaluate(self.snapshot, self.config, self.risk)
        self.assertEqual(result, evaluate(self.snapshot, self.config, self.risk))
        self.assertTrue(result.eligible)
        self.assertEqual(result.selected.buckets, ("18",))
        self.assertAlmostEqual(result.selected.cost_per_share_set, 0.25)
        self.assertAlmostEqual(
            result.selected.net_edge, result.selected.weather_probability - 0.25 - 0.015
        )
        self.assertAlmostEqual(
            result.selected.blended_probability,
            0.45 * result.selected.weather_probability + 0.55 * result.selected.market_probability,
        )

    def test_risk_vetoes_good_signal_at_exact_thresholds(self):
        for state, reason in (
            (replace(self.risk, daily_realized_usd=-5), "daily_loss_limit"),
            (replace(self.risk, consecutive_losses=3), "consecutive_loss_limit"),
            (replace(self.risk, equity_usd=70), "drawdown_halt"),
            (None, "risk_state_missing"),
        ):
            result = evaluate(self.snapshot, self.config, state)
            self.assertFalse(result.eligible)
            self.assertIn(reason, result.reasons)
            self.assertIsNotNone(result.selected)

    def test_bad_quotes_closed_market_and_future_model_are_blocked(self):
        for change, reason in (
            ({"closes_at": self.snapshot.as_of}, "market_closed"),
            (
                {
                    "forecast": replace(
                        self.snapshot.forecast, available_at="2026-11-16T12:01:00+08:00"
                    )
                },
                "model_not_available_at_snapshot",
            ),
            (
                {"forecast": replace(self.snapshot.forecast, disagreement_c=6)},
                "extreme_model_disagreement",
            ),
        ):
            result = evaluate(replace(self.snapshot, **change), self.config, self.risk)
            self.assertFalse(result.eligible)
            self.assertIn(reason, result.reasons)
        buckets = tuple(
            replace(b, quoted_at="2026-11-16T11:00:00+08:00") for b in self.snapshot.buckets
        )
        self.assertFalse(
            evaluate(replace(self.snapshot, buckets=buckets), self.config, self.risk).eligible
        )

    def test_document_replay_retains_missing_mode_rejection(self):
        source = json.loads(
            (Path(__file__).resolve().parents[1] / "doc/strategy_680665_config.json").read_text(
                encoding="utf-8"
            )
        )
        result = evaluate(document_snapshot(source), self.config)
        combo = next(c for c in result.candidates if c.buckets == ("16°C", "17°C"))
        self.assertIn("market_mode_not_in_combo", combo.reasons)
        self.assertIn("blended_probability_below_threshold", combo.reasons)
        self.assertFalse(result.eligible)
        self.assertEqual(result.weather_mode, "18°C")

    def test_config_version_changes_and_invalid_weight_is_rejected(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "strategy.json"
            self.raw_config["research_assumptions"]["market_weight"] = 0.6
            path.write_text(json.dumps(self.raw_config), encoding="utf-8")
            config, _ = load_config(path)
            self.assertNotEqual(config.version, self.config.version)
            self.raw_config["research_assumptions"]["market_weight"] = 0.86
            path.write_text(json.dumps(self.raw_config), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "cap"):
                load_config(path)


if __name__ == "__main__":
    unittest.main()
