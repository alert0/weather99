"""Loopback-only, read-only dashboard over the existing collector and paper ledger."""

import json
import sqlite3
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from .paper import DEFAULT_RUN_DIR, status
from .paper_report import evaluation_context, evaluation_lines, explain
from .nowcast import realtime_sources

ASSETS = Path(__file__).with_name("web_assets")


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}


def scan_view(row, run_dir):
    raw, signal, config = evaluation_context(row, run_dir)
    probabilities = {p[0]: p[1:] for p in signal.get("bucket_probabilities", [])}
    candidate = row.get("selected") or row.get("best_candidate") or {}
    keys = candidate.get("buckets", [])
    quotes = []
    for bucket in raw.get("buckets", []):
        key = bucket["token_id"]
        weather, market = probabilities.get(key, (None, None))
        quotes.append(
            {
                "label": bucket.get("bucket_label"),
                "bid": bucket.get("bid"),
                "ask": bucket.get("ask"),
                "weather_probability": weather,
                "market_probability": market,
                "candidate": key in keys,
                "market_mode": key == signal.get("market_mode"),
            }
        )
    return {
        "date": row["target_date"],
        "layer": row.get("layer"),
        "status": row.get("status"),
        "forecast": row.get("forecast"),
        "collected_at": raw.get("collected_at"),
        "models": raw.get("forecast", {}).get("models_c", {}),
        "realtime": realtime_sources(raw) if raw.get("collected_at") else {},
        "correction": row.get("correction"),
        "hourly_shadow": row.get("hourly_shadow"),
        "settlement": raw.get("settlement") or {"status": "not_collected", "observations": []},
        "quotes": quotes,
        "confirmation_count": row.get("confirmation_count", 0),
        "required_confirmations": signal.get("required_confirmations"),
        "probability_threshold": config.get("entry", {})
        .get("probability_threshold", {})
        .get(row.get("layer")),
        "probability": candidate.get("blended_probability"),
        "details": evaluation_lines(row, (raw, signal, config))[1:],
        "candidate_labels": [q["label"] for q in quotes if q["candidate"]],
        "candidate_valid": not candidate.get("reasons") if candidate else False,
    }


def ledger_view(run_dir):
    path = Path(run_dir) / "paper.sqlite3"
    if not path.exists():
        return {"positions": [], "history": [], "logs": []}
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        positions = [
            dict(r)
            for r in db.execute("SELECT * FROM paper_positions ORDER BY opened_at DESC LIMIT 100")
        ]
        for p in positions:
            # Token identifiers and model hashes have no display value.
            p["bucket_count"] = len(json.loads(p.pop("legs")))
            for key in ("id", "signal_id", "model_version", "config_version"):
                p.pop(key, None)
            for key in ("cost", "reserve", "payout"):
                p[key] = p[key] / 1_000_000 if p[key] is not None else None
        history = []
        times = [
            r[0]
            for r in db.execute(
                "SELECT DISTINCT as_of FROM paper_cycles ORDER BY as_of DESC LIMIT 240"
            )
        ][::-1]
        for moment in times:
            cash = db.execute(
                "SELECT COALESCE(SUM(cash_delta),0) FROM paper_cashflows WHERE as_of<=?", (moment,)
            ).fetchone()[0]
            marks = db.execute(
                """SELECT COALESCE(SUM(COALESCE((SELECT value FROM paper_marks m
                WHERE m.position_id=p.id AND m.as_of<=? ORDER BY m.as_of DESC LIMIT 1),0)),0)
                FROM paper_positions p WHERE p.opened_at<=? AND (p.settled_at IS NULL OR p.settled_at>?)""",
                (moment, moment, moment),
            ).fetchone()[0]
            history.append({"time": moment, "equity": (cash + marks) / 1_000_000})
        logs = []
        for record in db.execute(
            "SELECT as_of,target_date,status,payload FROM paper_cycles ORDER BY id DESC LIMIT 80"
        ):
            payload = json.loads(record["payload"])
            messages = list(dict.fromkeys(explain(r) for r in payload.get("reasons", [])))
            logs.append(
                {
                    "time": record["as_of"],
                    "date": record["target_date"],
                    "status": record["status"],
                    "message": "；".join(messages) or "本轮评估完成",
                }
            )
    return {"positions": positions, "history": history, "logs": logs}


def dashboard(run_dir=DEFAULT_RUN_DIR):
    state = status(run_dir)
    weather_meta = state.get("latest_weather") or {}
    weather = read_json(weather_meta.get("snapshot"))
    points = []
    for p in weather.get("points", []):
        current = p.get("current") or {}
        location = p.get("location") or {}
        points.append(
            {
                "id": p.get("point_id"),
                "name": location.get("name"),
                "latitude": location.get("latitude"),
                "longitude": location.get("longitude"),
                "complete": p.get("complete"),
                "temperature": current.get("temperature_c"),
                "observed_at": current.get("observed_at"),
                "condition": (current.get("condition") or {}).get("text"),
            }
        )
    return {
        "schema": "weather99.dashboard.v1",
        "server_time": datetime.now(UTC).isoformat(),
        "phase": state.get("phase"),
        "healthy": state.get("healthy", False),
        "heartbeat": state.get("heartbeat"),
        "next_scan_at": state.get("next_scan_at"),
        "interval_seconds": state.get("interval_seconds"),
        "started_at": state.get("started_at"),
        "model_mode": state.get("model_mode"),
        "account": state.get("account"),
        "weather": {
            "complete": weather_meta.get("complete"),
            "collected_at": weather_meta.get("collected_at"),
            "points": points,
            "daily_forecast": {key: value for key, value in weather.get("daily_forecast", {}).items()
                               if key != "sources"},
            "attributions": weather.get("attributions", []),
        },
        "scans": [scan_view(row, run_dir) for row in state.get("last_cycle") or []],
        **ledger_view(run_dir),
    }


def handler_for(run_dir):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            # No filesystem browsing, credentials, raw provider responses or control endpoints.
            if self.headers.get("Host") not in {
                f"127.0.0.1:{self.server.server_port}",
                f"localhost:{self.server.server_port}",
            }:
                self.send_error(403)
                return
            route = urlsplit(self.path).path
            files = {
                "/": ("index.html", "text/html"),
                "/app.js": ("app.js", "text/javascript"),
                "/settlement.js": ("settlement.js", "text/javascript"),
                "/style.css": ("style.css", "text/css"),
            }
            try:
                if route == "/api/dashboard":
                    data = json.dumps(
                        dashboard(run_dir), ensure_ascii=False, allow_nan=False
                    ).encode("utf-8")
                    content_type = "application/json"
                elif route in files:
                    filename, content_type = files[route]
                    data = (ASSETS / filename).read_bytes()
                else:
                    self.send_error(404)
                    return
            except (OSError, ValueError, KeyError, sqlite3.Error):
                self.send_error(503, "Local data is temporarily unavailable")
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type + "; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'",
            )
            self.end_headers()
            self.wfile.write(data)

    return Handler


def serve(run_dir=DEFAULT_RUN_DIR, port=8765):
    with ThreadingHTTPServer(("127.0.0.1", port), handler_for(run_dir)) as server:
        print(f"Weather99 dashboard: http://127.0.0.1:{port}", flush=True)
        server.serve_forever()
