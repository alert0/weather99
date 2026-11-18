"""Persistent paper runner. All exchange operations are GET-only data collection."""

import json
import logging
import math
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from statistics import fmean

from .adapters import live_snapshot
from .config import load_config
from .domain import SHANGHAI, ForecastEstimate, digest, finite, model_vector, timestamp
from .forecasting import ModelArtifact
from .ingest import collect
from .nowcast import correct_forecast
from .hourly_nowcast import hourly_tmax_shadow
from .paper_ledger import PaperLedger
from .qweather import QWeatherConfig, collect_airport_qweather
from .service import analyze
from .storage import AuditStore

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PAPER_CONFIG = ROOT / "config/paper.json"
DEFAULT_RUN_DIR = ROOT / "runtime/paper"
LOGGER = logging.getLogger(__name__)


def read_settings(path):
    settings = json.loads(path.read_text(encoding="utf-8"))
    if settings["schema"] != "weather99.paper.v1":
        raise ValueError("unsupported paper settings schema")
    for name in ("initial_cash_usd", "position_budget_usd", "baseline_sigma_c"):
        finite(settings[name], name)
        if settings[name] <= 0:
            raise ValueError(f"{name} must be positive")
    for name in ("shares_per_bucket", "max_open_positions", "interval_seconds"):
        if type(settings[name]) is not int or settings[name] < 1:
            raise ValueError(f"{name} must be a positive integer")
    if settings["interval_seconds"] < 60:
        raise ValueError("paper polling interval must be at least 60 seconds")
    if settings["model_mode"] not in ("equal_weight_baseline", "trained"):
        raise ValueError("unsupported paper model mode")
    if not settings["target_offsets_days"] or any(
        type(offset) is not int or offset not in (0, 1)
        for offset in settings["target_offsets_days"]
    ):
        raise ValueError("paper targets support only today and tomorrow")
    return settings


@dataclass(frozen=True)
class PaperBaseline:
    layer: str
    available_at: str
    sigma_c: float

    @property
    def definition(self):
        return {
            "kind": "uncalibrated_equal_weight_paper_v1",
            "layer": self.layer,
            "sigma_floor_c": self.sigma_c,
            "disagreement_coefficient": 0.072,
        }

    @property
    def version(self):
        return digest(self.definition)

    def predict(self, models):
        values = model_vector(models)
        center = fmean(values)
        disagreement = max(values) - min(values)
        return ForecastEstimate(
            center,
            center,
            center,
            math.sqrt(self.sigma_c**2 + 0.072 * disagreement**2),
            disagreement,
            self.version,
            "uncalibrated_equal_weight_paper_v1",
            self.available_at,
        )


def layer_for_event(raw):
    event = raw.get("sources", {}).get("event", {}).get("data") or {}
    end = event.get("endDate")
    if not end:
        return "24h"
    hours = (timestamp(end) - timestamp(raw["collected_at"])).total_seconds() / 3600
    for ceiling, layer in ((3, "3h"), (6, "6h"), (12, "12h")):
        if hours <= ceiling:
            return layer
    return "24h"


def atomic_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8"
    )
    os.replace(temporary, path)


class SingleInstance:
    def __init__(self, path):
        self.path = path

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a+b")
        self.file.write(b"0")
        self.file.flush()
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise ValueError("a paper runner is already using this run directory") from None
        return self

    def __exit__(self, *args):
        self.file.close()


def process_capture(raw, capture_path, settings, strategy, strategy_raw, ledger, audit_path):
    as_of, target = raw["collected_at"], raw["forecast"]["date"]
    layer = layer_for_event(raw)
    base = {
        "target_date": target,
        "layer": layer,
        "snapshot": str(capture_path),
        "model_mode": settings["model_mode"],
        "execution_enabled": False,
    }
    settled = ledger.settle(raw)
    ledger.mark(raw, strategy.quote_max_age_seconds)
    if settled:
        base["settlements"] = settled
    try:
        if raw.get("errors"):
            raise ValueError("capture_sources_failed")
        event = raw.get("sources", {}).get("event", {}).get("data") or {}
        rules = str(event.get("description", "")) + str(event.get("resolutionSource", ""))
        if not any(station in rules.lower() for station in ("zspd", "pudong")):
            raise ValueError("airport_settlement_rules_unverified")
        if "settlement" in raw and not raw["settlement"].get("rules_verified"):
            raise ValueError("settlement_rules_unsupported")
        if not ledger.account(as_of)["valuation_complete"]:
            raise ValueError("existing_positions_unpriced")
        if settings["model_mode"] == "trained":
            model_path = settings.get("trained_models", {}).get(layer)
            if not model_path:
                raise ValueError(f"trained_model_missing:{layer}")
            model = ModelArtifact.load(Path(model_path))
        else:
            model = PaperBaseline(
                layer, raw["sources"]["forecast"]["fetched_at"], settings["baseline_sigma_c"]
            )
            with AuditStore(audit_path) as store:
                store.register(model.version, "paper_baseline", model.definition)
        snapshot = live_snapshot(raw, model, layer)
        baseline_forecast = model.predict(raw["forecast"]["models_c"])
        _, correction = correct_forecast(raw, baseline_forecast, snapshot.observed_max_c)
        # Auxiliary research must never veto the existing Paper evaluation.
        try:
            settlement = raw.get("settlement") or {}
            verified_high = None
            if (
                settlement.get("rules_verified") is True
                and settlement.get("station") == snapshot.station
                and settlement.get("target_date") == target
            ):
                verified_high = snapshot.observed_max_c
            hourly_shadow = hourly_tmax_shadow(raw, baseline_forecast, verified_high)
        except Exception:
            LOGGER.exception("non-trading hourly shadow failed; Paper path continues")
            hourly_shadow = {
                "schema": "weather99.hourly_shadow.v1",
                "status": "shadow_computation_failed",
                "trade_eligible": False,
                "hours": [],
            }
        if correction["status"] == "applied":
            with AuditStore(audit_path) as store:
                store.register(snapshot.forecast.model_version, "intraday_correction", {
                    "base_model_version": model.version, "parameters": correction["parameters"],
                })
        signal = analyze(
            snapshot,
            strategy,
            strategy_raw,
            ledger.risk(as_of),
            raw,
            audit_path,
            artifact=model if isinstance(model, ModelArtifact) else None,
        )
        fill = ledger.buy(signal, snapshot, raw, settings, strategy)
        if fill["status"] == "paper_filled":
            ledger.mark(raw, strategy.quote_max_age_seconds)
        result = {
            **base,
            "status": fill["status"] if fill["status"] == "paper_filled" else signal["status"],
            "signal_id": signal["id"],
            "reasons": signal["reasons"],
            "fill": fill,
            "confirmation_count": signal["confirmation_count"],
            "model_version": snapshot.forecast.model_version,
            "forecast": {
                "center_c": snapshot.forecast.center_c,
                "sigma_c": snapshot.forecast.sigma_c,
                "method": snapshot.forecast.method,
            },
            "correction": correction,
            "hourly_shadow": hourly_shadow,
            "selected": signal["selected"],
            "best_candidate": signal["candidates"][0] if signal["candidates"] else None,
        }
    except (ValueError, KeyError, TypeError, OSError) as exc:
        with AuditStore(audit_path) as store:
            store.invalidate_confirmation(f"ZSPD:{target}:{layer}", as_of)
        result = {
            **base,
            "status": "blocked",
            "reasons": [str(exc)],
            "source_errors": raw.get("errors", {}),
        }
    ledger.record_cycle(target, as_of, result)
    return result


def run(run_dir=DEFAULT_RUN_DIR, settings_path=DEFAULT_PAPER_CONFIG, *, once=False):
    settings = read_settings(settings_path)
    strategy, strategy_raw = load_config()
    run_dir.mkdir(parents=True, exist_ok=True)
    stop_file = run_dir / "stop.request"
    status_path = run_dir / "status.json"
    with (
        SingleInstance(run_dir / "runner.lock"),
        PaperLedger(run_dir / "paper.sqlite3", settings["initial_cash_usd"]) as ledger,
    ):
        stop_file.unlink(missing_ok=True)
        started = datetime.now(UTC).isoformat()
        state = {
            "pid": os.getpid(),
            "started_at": started,
            "mode": "paper",
            "model_mode": settings["model_mode"],
            "settings_version": digest(settings),
            "interval_seconds": settings["interval_seconds"],
            "execution_enabled": False,
        }
        try:
            while not stop_file.exists():
                as_of = datetime.now(UTC).isoformat()
                atomic_json(
                    status_path,
                    state
                    | {"phase": "collecting", "heartbeat": as_of, "account": ledger.account(as_of)},
                )
                today = datetime.now(SHANGHAI).date()
                targets = {
                    str(today + timedelta(days=offset))
                    for offset in settings["target_offsets_days"]
                }
                targets.update(position["target_date"] for position in ledger.positions())
                weather_bundle = None
                try:
                    credentials = QWeatherConfig.from_env()
                    if credentials:
                        weather_bundle = collect_airport_qweather(credentials, str(today), include_forecast=True)
                        weather_path = (
                            run_dir
                            / "weather"
                            / (datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f") + ".json")
                        )
                        weather_path.parent.mkdir(parents=True, exist_ok=True)
                        with weather_path.open("x", encoding="utf-8") as handle:
                            json.dump(weather_bundle, handle, ensure_ascii=False, allow_nan=False)
                        valid_times = weather_bundle["summary"]["current_valid_times"]
                        prior_times = state.get("latest_weather", {}).get("valid_times", [])
                        state["latest_weather"] = {
                            "snapshot": str(weather_path),
                            "complete": weather_bundle["complete"],
                            "collected_at": datetime.now(UTC).isoformat(),
                            "valid_times": valid_times,
                            "observation_time_changed": bool(valid_times)
                            and valid_times != prior_times,
                            "summary": weather_bundle["summary"],
                            "errors": weather_bundle["errors"],
                        }
                        state.pop("weather_error", None)
                except Exception as exc:
                    LOGGER.exception("paper QWeather sampling failed")
                    state["weather_error"] = f"{type(exc).__name__}:{exc}"
                results = []
                for target in sorted(targets):
                    if stop_file.exists():
                        break
                    try:
                        capture_path, raw = collect(
                            target,
                            run_dir / "snapshots",
                            auxiliary_weather={"qweather": weather_bundle}
                            if weather_bundle
                            else None,
                        )
                        result = process_capture(
                            raw,
                            capture_path,
                            settings,
                            strategy,
                            strategy_raw,
                            ledger,
                            run_dir / "audit.sqlite3",
                        )
                    except Exception as exc:
                        LOGGER.exception("paper scan failed for %s", target)
                        as_of = datetime.now(UTC).isoformat()
                        result = {
                            "target_date": target,
                            "status": "error",
                            "reasons": [f"{type(exc).__name__}:{exc}"],
                        }
                        for layer in ("24h", "12h", "6h", "3h"):
                            with AuditStore(run_dir / "audit.sqlite3") as audit:
                                audit.invalidate_confirmation(f"ZSPD:{target}:{layer}", as_of)
                        ledger.record_cycle(target, as_of, result)
                    results.append(result)
                as_of = datetime.now(UTC).isoformat()
                state.update(
                    heartbeat=as_of,
                    phase="waiting",
                    last_cycle=results,
                    account=ledger.account(as_of),
                    positions=ledger.positions(),
                    next_scan_at=(
                        datetime.now(UTC) + timedelta(seconds=settings["interval_seconds"])
                    ).isoformat(),
                )
                atomic_json(status_path, state)
                print(json.dumps(state, ensure_ascii=False, allow_nan=False), flush=True)
                if once:
                    break
                deadline = time.monotonic() + settings["interval_seconds"]
                heartbeat_deadline = time.monotonic() + 15
                while time.monotonic() < deadline and not stop_file.exists():
                    if time.monotonic() >= heartbeat_deadline:
                        state["heartbeat"] = datetime.now(UTC).isoformat()
                        atomic_json(status_path, state)
                        heartbeat_deadline = time.monotonic() + 15
                    time.sleep(min(1, max(0, deadline - time.monotonic())))
        finally:
            state.update(phase="stopped", heartbeat=datetime.now(UTC).isoformat())
            atomic_json(status_path, state)
    return state


def status(run_dir=DEFAULT_RUN_DIR):
    path = run_dir / "status.json"
    if not path.exists():
        return {"phase": "not_started"}
    state = json.loads(path.read_text(encoding="utf-8"))
    age = (datetime.now(UTC) - timestamp(state["heartbeat"])).total_seconds()
    state["heartbeat_age_seconds"] = round(age, 1)
    state["healthy"] = state["phase"] != "stopped" and age <= 180
    return state
