"""Contract test between orchestration and P2's package. Skipped when 'inference' is not importable."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("inference")

from src import serializer as s  # noqa: E402
from src.inference_adapter import LilitInference, PeerObservation  # noqa: E402


def example_metrics() -> dict:
    return json.loads((s.contracts_dir() / "examples" / "metrics.example.json").read_text())


def test_local_state_from_v020_metrics_is_contract_valid():
    state = LilitInference().local_state(example_metrics())
    s.validate_state(state)
    assert state["scope"] == "local" and state["contributing_peers"] == []
    assert state["schema_version"] == s.SUPPORTED_SCHEMA_VERSION
    assert state["timestamp"] == example_metrics()["interval_end"]


def test_regional_state_merges_fresh_peer_and_ignores_stale_peer():
    adapter = LilitInference(peer_max_age_s=30)
    metrics = example_metrics()
    local = adapter.local_state(metrics)
    now = datetime.now(timezone.utc)
    fresh = PeerObservation("node-b", 0.9, 0.9, 1, now, now)
    old = now - timedelta(seconds=120)
    stale = PeerObservation("node-c", 0.9, 0.9, 1, old, old)
    regional = adapter.regional_state(metrics, local, [fresh, stale], now=now)
    s.validate_state(regional)
    assert regional["scope"] == "regional" and regional["contributing_peers"] == ["node-b"]