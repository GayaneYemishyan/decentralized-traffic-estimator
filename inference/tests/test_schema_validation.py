import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from inference.ontology import LaneObservation, NodeMetricsSnapshot, Scope, TemporalWindow
from inference.schema_validator import (
    SchemaValidationError,
    _validate_node,
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


def test_fallback_validator_resolves_ref_and_local_scope_peers():
    schema = {
        "$defs": {
            "id": {"type": "string", "pattern": r"^node-[a-z0-9-]+$"},
        },
        "type": "object",
        "required": ["node_id", "scope", "contributing_peers"],
        "properties": {
            "node_id": {"$ref": "#/$defs/id"},
            "scope": {"type": "string", "enum": ["local", "regional"]},
            "contributing_peers": {"type": "array"},
        },
        "if": {
            "properties": {"scope": {"const": "local"}},
            "required": ["scope"],
        },
        "then": {
            "properties": {"contributing_peers": {"maxItems": 0}},
        },
    }

    valid = {"node_id": "node-1", "scope": "local", "contributing_peers": []}
    errors: list[str] = []
    _validate_node(valid, schema, schema, "$", errors)
    assert errors == []

    bad_id = {"node_id": "BAD_NODE", "scope": "local", "contributing_peers": []}
    bad_id_errors: list[str] = []
    _validate_node(bad_id, schema, schema, "$", bad_id_errors)
    assert any("$.node_id" in err for err in bad_id_errors)

    bad_peers = {"node_id": "node-1", "scope": "local", "contributing_peers": ["peer-1"]}
    bad_peer_errors: list[str] = []
    _validate_node(bad_peers, schema, schema, "$", bad_peer_errors)
    assert any("contributing_peers" in err for err in bad_peer_errors)


def test_validate_metrics_payload_checks_lane_counts_sum(tmp_path: Path):
    _write_contracts(tmp_path)
    valid_metrics = {
        "schema_version": "0.2.0",
        "node_id": "node-1",
        "seq": 1,
        "lanes": [
            {
                "lane_id": "lane-1",
                "vehicle_count": 5,
                "counts_by_class": {"car": 3, "motorcycle": 1, "bus": 0, "truck": 1},
            }
        ],
    }
    validate_metrics_payload(valid_metrics, contracts_dir=tmp_path)

    mismatched_metrics = {
        "schema_version": "0.2.0",
        "node_id": "node-1",
        "seq": 2,
        "lanes": [
            {
                "lane_id": "lane-1",
                "vehicle_count": 6,
                "counts_by_class": {"car": 3, "motorcycle": 1, "bus": 0, "truck": 1},
            }
        ],
    }
    with pytest.raises(SchemaValidationError, match="vehicle_count"):
        validate_metrics_payload(mismatched_metrics, contracts_dir=tmp_path)
