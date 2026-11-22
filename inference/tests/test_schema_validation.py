import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from inference.ontology import LaneObservation, NodeMetricsSnapshot, Scope, TemporalWindow
from inference.schema_validator import (
    SchemaValidationError,
    validate_metrics_payload,
    validate_state_payload,
)
from inference.smoother import TemporalSmoother
from inference.state_builder import build_state_payload


def _write_contracts(contracts_dir: Path) -> None:
    metrics_schema = {
        "type": "object",
        "required": ["schema_version", "node_id", "seq", "lanes"],
        "additionalProperties": False,
        "properties": {
            "schema_version": {"type": "string", "minLength": 1},
            "node_id": {"type": "string", "minLength": 1},
            "seq": {"type": "integer", "minimum": 0},
            "lanes": {"type": "array", "minItems": 1},
        },
    }
    state_schema = {
        "type": "object",
        "required": [
            "schema_version",
            "node_id",
            "seq",
            "timestamp",
            "state",
            "congestion_score",
            "confidence",
            "scope",
            "contributing_peers",
        ],
        "additionalProperties": False,
        "properties": {
            "schema_version": {"type": "string", "minLength": 1},
            "node_id": {"type": "string", "minLength": 1},
            "seq": {"type": "integer", "minimum": 0},
            "timestamp": {"type": "string", "format": "date-time"},
            "state": {"type": "string", "enum": ["free", "moderate", "congested"]},
            "congestion_score": {"type": "number", "minimum": 0.0, "maximum": 1.0},
            "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
            "scope": {"type": "string", "enum": ["local", "regional"]},
            "contributing_peers": {"type": "array", "maxItems": 64, "uniqueItems": True},
        },
    }
    (contracts_dir / "metrics_schema.json").write_text(json.dumps(metrics_schema), encoding="utf-8")
    (contracts_dir / "state_schema.json").write_text(json.dumps(state_schema), encoding="utf-8")


def test_valid_state_payload_passes_schema(tmp_path: Path):
    _write_contracts(tmp_path)
    win = TemporalWindow(
        start=datetime(2026, 11, 22, 12, 0, 0, tzinfo=timezone.utc),
        end=datetime(2026, 11, 22, 12, 0, 10, tzinfo=timezone.utc),
    )
    snap = NodeMetricsSnapshot(
        schema_version="0.1.0",
        node_id="node-1",
        seq=1,
        window=win,
        source="synthetic",
        lanes=[
            LaneObservation(
                lane_id="lane-1",
                vehicle_count=5,
                flow_rate_vph=1000.0,
                avg_occupancy=0.30,
            )
        ],
    )
    smoother = TemporalSmoother(alpha=1.0)
    ev = smoother.update(snap)
    payload = build_state_payload(snap, ev, scope=Scope.LOCAL)

    validate_state_payload(payload, contracts_dir=tmp_path)


def test_invalid_state_payload_raises(tmp_path: Path):
    _write_contracts(tmp_path)
    bad_payload = {
        "schema_version": "0.1.0",
        "node_id": "node-1",
        "seq": 1,
        "timestamp": "2026-11-22T12:00:10.000Z",
        "state": "unknown_state",
        "congestion_score": 1.5,
        "confidence": 0.8,
        "scope": "local",
        "contributing_peers": [],
    }
    with pytest.raises(SchemaValidationError):
        validate_state_payload(bad_payload, contracts_dir=tmp_path)


def test_invalid_metrics_payload_missing_required_raises(tmp_path: Path):
    _write_contracts(tmp_path)
    incomplete_metrics = {
        "schema_version": "0.1.0",
        "node_id": "node-1",
    }
    with pytest.raises(SchemaValidationError):
        validate_metrics_payload(incomplete_metrics, contracts_dir=tmp_path)
