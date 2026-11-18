"""CLI for collection, point-in-time training and replayable research signals."""

import argparse
import json
import sys
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .adapters import document_snapshot
from .config import DEFAULT_CONFIG, LAYERS, load_config
from .domain import RiskState
from .forecasting import ModelArtifact, TrainingOptions, fit, load_samples, walk_forward
from .ingest import ROOT, collect
from .locations import PUDONG
from .paper import DEFAULT_PAPER_CONFIG, DEFAULT_RUN_DIR
from .paper import run as run_paper
from .paper import status as paper_status
from .qweather import QWeatherConfig, collect_airport_qweather, collect_qweather
from .service import analyze, analyze_capture


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def emit(value):
    print(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))


def weather_report(value):
    """Keep raw per-point responses in the snapshot, not in terminal summaries."""
    if isinstance(value, dict):
        return {key: weather_report(item) for key, item in value.items() if key != "sources"}
    if isinstance(value, list):
        return [weather_report(item) for item in value]
    return value


def report(result):
    rows = result["candidates"]
    return {
        key: result[key]
        for key in (
            "id",
            "status",
            "reasons",
            "config_version",
            "model_version",
            "confirmation_count",
            "required_confirmations",
            "execution_enabled",
        )
    } | {
        "weather_mode": result["weather_mode"],
        "market_mode": result["market_mode"],
        "selected": result["selected"],
        "best_candidate": rows[0] if rows else None,
        "evaluated_combinations": len(rows),
    }


def build_parser():
    parser = argparse.ArgumentParser(description="Weather99 weather probability research engine")
    commands = parser.add_subparsers(dest="command", required=True)
    web = commands.add_parser("web", help="serve the local live-data dashboard")
    web.add_argument("--port", type=int, default=8765)
    web.add_argument("--run-dir", type=Path, default=DEFAULT_RUN_DIR)
    collect_parser = commands.add_parser("collect", help="capture public source responses")
    collect_parser.add_argument(
        "--date", type=date.fromisoformat, default=datetime.now(timezone(timedelta(hours=8))).date()
    )
    collect_parser.add_argument("--output", type=Path, default=ROOT / "runtime/snapshots")
    collect_parser.add_argument(
        "--with-qweather",
        action="store_true",
        help="require QWeather credentials and add auxiliary weather",
    )
    qweather = commands.add_parser("qweather", help="fetch QWeather current grid temperature")
    qweather.add_argument(
        "--date", type=date.fromisoformat, default=datetime.now(timezone(timedelta(hours=8))).date()
    )
    qweather.add_argument(
        "--single-point", action="store_true", help="query only the airport reference coordinate"
    )
    qweather.add_argument("--latitude", type=float, help="custom coordinate; requires --longitude")
    qweather.add_argument("--longitude", type=float, help="custom coordinate; requires --latitude")
    paper = commands.add_parser("paper", help="run, inspect or stop persistent simulated trading")
    paper_commands = paper.add_subparsers(dest="paper_command", required=True)
    for command in ("run", "status", "stop"):
        child = paper_commands.add_parser(command)
        child.add_argument("--run-dir", type=Path, default=DEFAULT_RUN_DIR)
        if command == "status":
            child.add_argument(
                "--human", action="store_true", help="show a readable Chinese summary"
            )
        if command == "run":
            child.add_argument("--paper-config", type=Path, default=DEFAULT_PAPER_CONFIG)
            child.add_argument("--once", action="store_true")
    train = commands.add_parser("train", help="fit immutable rolling calibration and ridge model")
    train.add_argument("--history", type=Path, required=True)
    train.add_argument("--as-of", required=True, help="timezone-aware training cutoff")
    train.add_argument("--output", type=Path, required=True)
    train.add_argument("--layer", choices=LAYERS, default="24h")
    validate = commands.add_parser("validate", help="walk-forward MAE on settled history")
    validate.add_argument("--history", type=Path, required=True)
    validate.add_argument("--days", type=int, default=180)
    validate.add_argument("--output", type=Path, required=True)
    validate.add_argument("--layer", choices=LAYERS, default="24h")
    replay = commands.add_parser("analyze", help="evaluate a saved capture; never place orders")
    replay.add_argument("--snapshot", type=Path, required=True)
    replay.add_argument("--model", type=Path, required=True)
    replay.add_argument("--layer", choices=LAYERS, default="24h")
    replay.add_argument("--risk", type=Path)
    demo = commands.add_parser(
        "demo", help="replay the documented FMZ snapshot without trained coefficients"
    )
    demo.add_argument("--document", type=Path, default=ROOT / "doc/strategy_680665_config.json")
    for child in (train, validate, replay, demo):
        child.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    for child in (replay, demo):
        child.add_argument("--database", type=Path, default=ROOT / "runtime/weather99.sqlite3")
    return parser


def main(argv=None):
    args_list = list(sys.argv[1:] if argv is None else argv)
    # Preserve the v0.1 `python -m weather99 --date ...` collection interface.
    if not args_list or args_list[0] == "--date" or args_list[0] == "--output":
        args_list.insert(0, "collect")
    parser = build_parser()
    args = parser.parse_args(args_list)
    try:
        if args.command == "web":
            from .web import serve

            serve(args.run_dir, args.port)
            return 0
        if args.command == "paper":
            if args.paper_command == "run":
                run_paper(args.run_dir, args.paper_config, once=args.once)
            elif args.paper_command == "status":
                state = paper_status(args.run_dir)
                if args.human:
                    from .paper_report import render_status

                    print(render_status(state, args.run_dir))
                else:
                    emit(state)
            else:
                args.run_dir.mkdir(parents=True, exist_ok=True)
                (args.run_dir / "stop.request").touch()
                emit({"status": "stop_requested", "run_dir": str(args.run_dir)})
            return 0
        if args.command == "collect":
            qweather = QWeatherConfig.from_env(required=args.with_qweather)
            path, snapshot = collect(str(args.date), args.output, qweather)
            emit(
                {
                    "snapshot": str(path),
                    "data_ready": snapshot["data_ready"],
                    "forecast": snapshot["forecast"],
                    "buckets": len(snapshot["buckets"]),
                    "errors": snapshot["errors"],
                    "auxiliary_weather": {
                        name: weather_report(payload)
                        for name, payload in snapshot.get("auxiliary_weather", {}).items()
                    },
                }
            )
            auxiliary_ready = (
                snapshot.get("auxiliary_weather", {}).get("qweather", {}).get("complete", False)
            )
            return (
                0 if snapshot["data_ready"] and (not args.with_qweather or auxiliary_ready) else 2
            )
        if args.command == "qweather":
            if (args.latitude is None) != (args.longitude is None):
                raise ValueError("--latitude and --longitude must be provided together")
            credentials = QWeatherConfig.from_env(required=True)
            if args.single_point or args.latitude is not None:
                result = collect_qweather(
                    credentials,
                    str(args.date),
                    PUDONG.latitude if args.latitude is None else args.latitude,
                    PUDONG.longitude if args.longitude is None else args.longitude,
                )
            else:
                result = collect_airport_qweather(credentials, str(args.date))
            emit(weather_report(result))
            return 0 if result["complete"] else 2
        config, raw_config = load_config(args.config)
        if args.command in ("train", "validate"):
            options = TrainingOptions(**raw_config["training"])
            samples = load_samples(args.history)
            if args.command == "train":
                model = fit(samples, args.as_of, options, args.layer)
                model.save(args.output)
                emit({"model": str(args.output), **asdict(model)})
            else:
                result = walk_forward(samples, options, args.days, args.layer)
                args.output.parent.mkdir(parents=True, exist_ok=True)
                with args.output.open("x", encoding="utf-8") as handle:
                    json.dump(result, handle, ensure_ascii=False, indent=2, allow_nan=False)
                emit({key: value for key, value in result.items() if key != "rows"})
            return 0
        if args.command == "demo":
            source = read_json(args.document)
            snapshot = document_snapshot(source)
            # Historical equity peak is unknown; missing risk is explicitly vetoed.
            result = analyze(snapshot, config, raw_config, None, source, args.database)
            emit({"mode": "document_replay_only", **report(result)})
        else:
            risk = RiskState(**read_json(args.risk)) if args.risk else None
            result = analyze_capture(
                read_json(args.snapshot),
                ModelArtifact.load(args.model),
                args.layer,
                config,
                raw_config,
                risk,
                args.database,
            )
            emit(report(result))
        return 0
    except (ValueError, KeyError, TypeError, OSError) as exc:
        emit({"status": "invalid_input", "error": str(exc), "execution_enabled": False})
        return 2
