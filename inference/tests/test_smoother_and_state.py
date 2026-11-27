import re
from datetime import datetime, timedelta, timezone

from inference.ontology import LaneObservation, NodeMetricsSnapshot, Scope, TemporalWindow, TrafficState
from inference.smoother import TemporalSmoother
from inference.state_builder import build_state_payload


def _snap(seq: int, occupancy: float, flow_vph: float, vehicles: int = 10):
    base = datetime(2026, 11, 22, 12, 0, 0, tzinfo=timezone.utc)
    win = TemporalWindow(
        start=base + timedelta(seconds=seq * 10),
        end=base + timedelta(seconds=seq * 10 + 10),
    )
    return NodeMetricsSnapshot(
        schema_version="0.1.0",
        node_id="node-1",
        seq=seq,
        window=win,
        source="synthetic",
        lanes=[
            LaneObservation(
                lane_id="lane-1",
                vehicle_count=vehicles,
                flow_rate_vph=flow_vph,
                avg_occupancy=occupancy,
            )
        ],
    )


def test_smoother_smooths_sudden_spike():
    smoother = TemporalSmoother(alpha=0.5)
    first = smoother.update(_snap(1, occupancy=0.10, flow_vph=400.0))
    second = smoother.update(_snap(2, occupancy=0.95, flow_vph=1900.0))

    assert first.state == TrafficState.FREE
    assert second.congestion_score < 0.90
    assert second.congestion_score > first.congestion_score


def test_local_scope_forces_empty_contributing_peers():
    smoother = TemporalSmoother(alpha=1.0)
    snap = _snap(1, occupancy=0.20, flow_vph=500.0)
    ev = smoother.update(snap)
    payload = build_state_payload(snap, ev, scope=Scope.LOCAL, contributing_peers=["peer-2"])

    assert payload["contributing_peers"] == []
    assert payload["scope"] == "local"
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$", str(payload["timestamp"]))


def test_regional_scope_deduplicates_peers():
    smoother = TemporalSmoother(alpha=1.0)
    snap = _snap(3, occupancy=0.85, flow_vph=1800.0)
    ev = smoother.update(snap)
    payload = build_state_payload(
        snap,
        ev,
        scope=Scope.REGIONAL,
        contributing_peers=["peer-b", "peer-a", "peer-b"],
    )

    assert payload["scope"] == "regional"
    assert payload["contributing_peers"] == ["peer-b", "peer-a"]


def test_hysteresis_reduces_oscillations_on_noisy_sequence():
    noisy_specs = [
        (0.30, 600.0),
        (0.42, 840.0),
        (0.28, 560.0),
        (0.45, 900.0),
        (0.29, 580.0),
        (0.44, 880.0),
    ]

    raw_smoother = TemporalSmoother(alpha=1.0, hysteresis=0.0, min_dwell_steps=1)
    hyst_smoother = TemporalSmoother(alpha=0.5, hysteresis=0.08, min_dwell_steps=2)

    raw_states = []
    hyst_states = []
    for idx, (occ, flow) in enumerate(noisy_specs, start=1):
        snap = _snap(idx, occupancy=occ, flow_vph=flow)
        raw_states.append(raw_smoother.update(snap).state)
        hyst_states.append(hyst_smoother.update(snap).state)

    raw_transitions = sum(1 for a, b in zip(raw_states, raw_states[1:]) if a != b)
    hyst_transitions = sum(1 for a, b in zip(hyst_states, hyst_states[1:]) if a != b)

    assert raw_transitions > 0
    assert hyst_transitions < raw_transitions