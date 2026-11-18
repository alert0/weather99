"""Application boundary: translate inputs, run the pure engine, atomically record results."""

from dataclasses import asdict
from pathlib import Path

from .adapters import live_snapshot
from .config import StrategyConfig
from .domain import MarketSnapshot, RiskState
from .forecasting import ModelArtifact
from .storage import AuditStore
from .strategy import evaluate


def analyze(
    snapshot: MarketSnapshot,
    config: StrategyConfig,
    config_raw: dict,
    risk: RiskState | None,
    source: dict,
    database: Path,
    artifact: ModelArtifact | None = None,
) -> dict:
    with AuditStore(database) as store:
        if artifact:
            store.register(artifact.version, "forecast_model", asdict(artifact))
        return store.record(
            snapshot, evaluate(snapshot, config, risk), config, risk, config_raw, source
        )


def analyze_capture(
    raw: dict,
    artifact: ModelArtifact,
    layer: str,
    config: StrategyConfig,
    config_raw: dict,
    risk: RiskState | None,
    database: Path,
) -> dict:
    return analyze(
        live_snapshot(raw, artifact, layer), config, config_raw, risk, raw, database, artifact
    )
