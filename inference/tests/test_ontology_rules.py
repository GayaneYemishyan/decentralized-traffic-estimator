from datetime import datetime, timezone

from inference.ontology import (
    LaneObservation,
    NodeMetricsSnapshot,
    SpatialCell,
    TemporalWindow,
    TrafficState,
)
from inference.rules import classify_state, evaluate_snapshot


def _make_snapshot(lanes, start_sec=0, end_sec=10):
    window = TemporalWindow(
        start=datetime(2026, 11, 20, 10, 0, start_sec, tzinfo=timezone.utc),
        end=datetime(2026, 11, 20, 10, 0, end_sec, tzinfo=timezone.utc),
    )
    return NodeMetricsSnapshot(
        schema_version="0.1.0",
        node_id="node-a",
        seq=1,
        window=window,
        source="synthetic",
        lanes=lanes,
        spatial_cell=SpatialCell(cell_id="cell-0-0", grid_x=0, grid_y=0),
    )


def test_free_traffic_state():
    snap = _make_snapshot([
        LaneObservation("lane-1", vehicle_count=2, flow_rate_vph=400.0, avg_occupancy=0.10),
    ])
    result = evaluate_snapshot(snap)
    assert result.state == TrafficState.FREE
    assert 0.0 <= result.congestion_score < 0.35
    assert 0.0 <= result.confidence <= 1.0


def test_moderate_traffic_state():
    snap = _make_snapshot([
        LaneObservation("lane-1", vehicle_count=6, flow_rate_vph=1440.0, avg_occupancy=0.35),
        LaneObservation("lane-2", vehicle_count=4, flow_rate_vph=720.0, avg_occupancy=0.25),
    ])
    result = evaluate_snapshot(snap)
    assert result.state == TrafficState.MODERATE
    assert 0.35 <= result.congestion_score < 0.70


def test_congested_traffic_state():
    snap = _make_snapshot([
        LaneObservation("lane-1", vehicle_count=15, flow_rate_vph=1800.0, avg_occupancy=0.90),
    ])
    result = evaluate_snapshot(snap)
    assert result.state == TrafficState.CONGESTED
    assert result.congestion_score >= 0.70


def test_score_bounds_clamped():
    assert classify_state(-0.5) == TrafficState.FREE
    assert classify_state(1.5) == TrafficState.CONGESTED
