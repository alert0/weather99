"""Append-only audit records and transactional confirmation state in a local SQLite store."""

import json
import sqlite3
from dataclasses import asdict
from pathlib import Path

from .config import StrategyConfig
from .domain import Evaluation, MarketSnapshot, RiskState, digest, timestamp


def encode(value) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)


class AuditStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, timeout=30, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA foreign_keys=ON;
            CREATE TABLE IF NOT EXISTS artifacts (
                id TEXT PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS evaluations (
                id TEXT PRIMARY KEY, scope TEXT NOT NULL, as_of TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS confirmation_state (
                scope TEXT PRIMARY KEY, as_of TEXT NOT NULL,
                signature TEXT NOT NULL, count INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS domain_events (
                id TEXT PRIMARY KEY REFERENCES evaluations(id),
                event_type TEXT NOT NULL, payload TEXT NOT NULL
            );
        """)

    def close(self):
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def register(self, artifact_id: str, kind: str, payload: dict):
        encoded = encode(payload)
        self.connection.execute(
            "INSERT OR IGNORE INTO artifacts VALUES (?, ?, ?)", (artifact_id, kind, encoded)
        )
        existing = self.connection.execute(
            "SELECT kind, payload FROM artifacts WHERE id=?", (artifact_id,)
        ).fetchone()
        if existing["kind"] != kind or existing["payload"] != encoded:
            raise ValueError("immutable artifact identifier reused with different content")

    def invalidate_confirmation(self, scope: str, as_of: str):
        """A missing/invalid scan breaks the consecutive-success requirement."""
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            previous = self.connection.execute(
                "SELECT as_of FROM confirmation_state WHERE scope=?", (scope,)
            ).fetchone()
            if previous is None or timestamp(as_of) > timestamp(previous["as_of"]):
                self.connection.execute(
                    """INSERT INTO confirmation_state VALUES (?, ?, '', 0)
                    ON CONFLICT(scope) DO UPDATE SET as_of=excluded.as_of, signature='', count=0""",
                    (scope, as_of),
                )
            self.connection.execute("COMMIT")
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise

    def record(
        self,
        snapshot: MarketSnapshot,
        evaluation: Evaluation,
        config: StrategyConfig,
        risk: RiskState | None,
        config_raw: dict,
        source_snapshot: dict,
    ) -> dict:
        input_payload = {
            "snapshot": asdict(snapshot),
            "risk": asdict(risk) if risk else None,
            "source_snapshot": source_snapshot,
        }
        evaluation_id = digest(
            {"input": input_payload, "config": config.version, "engine": "weather99.signal.v1"}
        )
        scope = f"{snapshot.station}:{snapshot.target_date}:{snapshot.layer}"
        connection = self.connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            existing = connection.execute(
                "SELECT payload FROM evaluations WHERE id=?", (evaluation_id,)
            ).fetchone()
            if existing:
                connection.execute("COMMIT")
                return json.loads(existing["payload"])
            self.register(config.version, "strategy_config", config_raw)
            self.register(digest(input_payload), "evaluation_input", input_payload)
            previous = connection.execute(
                "SELECT * FROM confirmation_state WHERE scope=?", (scope,)
            ).fetchone()
            selected = evaluation.selected
            signature = digest(
                {
                    "config": config.version,
                    "model": evaluation.model_version,
                    "buckets": selected.buckets if selected else None,
                }
            )
            count = 0
            reasons = list(evaluation.reasons)
            eligible = evaluation.eligible
            forward = previous is None or timestamp(snapshot.as_of) > timestamp(previous["as_of"])
            if not forward:
                reasons.append("non_increasing_snapshot_time")
                eligible = False
            if eligible:
                count = 1
                if previous and previous["signature"] == signature:
                    gap = (timestamp(snapshot.as_of) - timestamp(previous["as_of"])).total_seconds()
                    if 0 < gap <= config.confirmation_max_gap_seconds:
                        count += previous["count"]
            if forward:
                connection.execute(
                    """INSERT INTO confirmation_state VALUES (?, ?, ?, ?)
                    ON CONFLICT(scope) DO UPDATE SET as_of=excluded.as_of,
                    signature=excluded.signature, count=excluded.count""",
                    (scope, snapshot.as_of, signature, count),
                )
            status = "rejected"
            if eligible:
                status = (
                    "confirmed_research_signal"
                    if count >= config.confirmations
                    else "pending_confirmation"
                )
            output = {
                **evaluation.to_dict(),
                "id": evaluation_id,
                "status": status,
                "eligible": eligible,
                "reasons": reasons,
                "confirmation_count": count,
                "required_confirmations": config.confirmations,
                "execution_enabled": False,
            }
            encoded = encode(output)
            connection.execute(
                "INSERT INTO evaluations VALUES (?, ?, ?, ?)",
                (evaluation_id, scope, snapshot.as_of, encoded),
            )
            connection.execute(
                "INSERT INTO domain_events VALUES (?, ?, ?)", (evaluation_id, status, encoded)
            )
            connection.execute("COMMIT")
            return json.loads(encoded)
        except BaseException:
            connection.execute("ROLLBACK")
            raise
