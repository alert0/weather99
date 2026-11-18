import io
import json
import unittest
from contextlib import redirect_stdout
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory

from tests.test_forecasting import synthetic_history
from weather99.cli import main
from weather99.domain import MODELS, timestamp
from weather99.forecasting import fit


class PipelineTests(unittest.TestCase):
    def test_train_save_load_and_capture_evaluation(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            history = []
            for sample in synthetic_history():
                history.append({**asdict(sample), "models_c": dict(zip(MODELS, sample.models_c))})
            history_path, model_path = root / "history.json", root / "model.json"
            history_path.write_text(json.dumps(history), encoding="utf-8")
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = main(
                    [
                        "train",
                        "--history",
                        str(history_path),
                        "--as-of",
                        "2026-09-24T08:00:00+08:00",
                        "--output",
                        str(model_path),
                    ]
                )
            self.assertEqual(code, 0, buffer.getvalue())
            artifact = fit(synthetic_history(), "2026-09-24T08:00:00+08:00")
            prediction = artifact.predict(dict(zip(MODELS, synthetic_history()[-1].models_c)))
            center = round(prediction.center_c)
            as_of = "2026-09-24T12:00:00+08:00"
            labels = (f"{center - 1}°C or below", f"{center}°C", f"{center + 1}°C or higher")
            buckets = [
                {
                    "token_id": str(index),
                    "bucket_label": label,
                    "bid": 0.14,
                    "ask": 0.15,
                    "market_probability": price,
                    "book_timestamp": int(timestamp(as_of).timestamp() * 1000),
                }
                for index, (label, price) in enumerate(zip(labels, (0.15, 0.70, 0.15)))
            ]
            slug = "highest-temperature-in-shanghai-on-september-25-2026"
            capture = {
                "schema": "weather99.snapshot.v1",
                "station": "ZSPD",
                "collected_at": as_of,
                "event_slug": slug,
                "errors": {},
                "forecast": {
                    "date": "2026-09-25",
                    "models_c": dict(zip(MODELS, synthetic_history()[-1].models_c)),
                },
                "buckets": buckets,
                "sources": {
                    "forecast": {"fetched_at": as_of},
                    "event": {
                        "fetched_at": as_of,
                        "data": {
                            "active": True,
                            "closed": False,
                            "slug": slug,
                            "endDate": "2026-09-26T00:00:00+08:00",
                        },
                    },
                },
            }
            capture_path = root / "capture.json"
            capture_path.write_text(json.dumps(capture), encoding="utf-8")
            risk_path = root / "risk.json"
            risk_path.write_text(
                json.dumps(
                    {
                        "trading_date": "2026-09-24",
                        "daily_realized_usd": 0,
                        "consecutive_losses": 0,
                        "equity_usd": 100,
                        "peak_equity_usd": 100,
                    }
                ),
                encoding="utf-8",
            )
            with redirect_stdout(buffer := io.StringIO()):
                code = main(
                    [
                        "analyze",
                        "--snapshot",
                        str(capture_path),
                        "--model",
                        str(model_path),
                        "--risk",
                        str(risk_path),
                        "--database",
                        str(root / "audit.sqlite3"),
                    ]
                )
            self.assertEqual(code, 0, buffer.getvalue())
            output = json.loads(buffer.getvalue())
            self.assertEqual(output["status"], "pending_confirmation")
            self.assertIsNotNone(output["selected"])
            self.assertFalse(output["execution_enabled"])

    def test_invalid_capture_fails_with_structured_error(self):
        with redirect_stdout(buffer := io.StringIO()):
            code = main(["analyze", "--snapshot", "does-not-exist", "--model", "does-not-exist"])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(buffer.getvalue())["status"], "invalid_input")


if __name__ == "__main__":
    unittest.main()
