from datetime import datetime, timezone

from inference.aggregator import (
    PeerStateObservation,
    aggregate_peer_states,
    build_regional_state_payload,
)
from inference.ontology import LaneObservation, NodeMetricsSnapshot, TemporalWindow, TrafficState
from inference.rules import RuleEvaluation


def _sample_snapshot() -> NodeMetricsSnapshot:
    win = TemporalWindow(
        start=datetime(2026, 11, 22, 12, 0, 0, tzinfo=timezone.utc),
        end=datetime(2026, 11, 22, 12, 0, 10, tzinfo=timezone.utc),
    )
    return NodeMetricsSnapshot(
        schema_version="0.1.0",
        node_id="node-local",
        seq=5,
        window=win,
        source="synthetic",
        lanes=[
            LaneObservation(
                lane_id="lane-1",
                vehicle_count=12,
                flow_rate_vph=1500.0,
                avg_occupancy=0.65,
            )
        ],
    )


def test_confidence_weighted_aggregation():
    local_eval = RuleEvaluation(
        congestion_score=0.30,
        confidence=0.90,
        state=TrafficState.FREE,
    )
    peers = [
        PeerStateObservation(peer_id="peer-high-conf", congestion_score=0.80, confidence=0.95),
        PeerStateObservation(peer_id="peer-low-conf", congestion_score=0.10, confidence=0.10),
    ]

    result = aggregate_peer_states(local_eval, peers, local_node_id="node-local")

    assert result.peer_count == 2
    assert result.contributing_peers == ["peer-high-conf", "peer-low-conf"]
    assert 0.45 <= result.evaluation.congestion_score <= 0.65


def test_agreement_increases_confidence_over_disagreement():
    local_eval = RuleEvaluation(
        congestion_score=0.75,
        confidence=0.70,
        state=TrafficState.CONGESTED,
    )
    agreeing_peers = [
        PeerStateObservation(peer_id="p1", congestion_score=0.76, confidence=0.75),
        PeerStateObservation(peer_id="p2", congestion_score=0.74, confidence=0.75),
    ]
    disagreeing_peers = [
        PeerStateObservation(peer_id="p1", congestion_score=0.10, confidence=0.75),
        PeerStateObservation(peer_id="p2", congestion_score=0.95, confidence=0.75),
    ]

    agreed = aggregate_peer_states(local_eval, agreeing_peers, local_node_id="node-local")
    disagreed = aggregate_peer_states(local_eval, disagreeing_peers, local_node_id="node-local")

    assert agreed.evaluation.confidence > disagreed.evaluation.confidence


def test_deduplicates_excludes_self_and_caps_at_64_peers():
    local_eval = RuleEvaluation(
        congestion_score=0.50,
        confidence=0.80,
        state=TrafficState.MODERATE,
    )
    peers = [
        PeerStateObservation(peer_id="node-local", congestion_score=0.90, confidence=0.99),
        PeerStateObservation(peer_id="peer-1", congestion_score=0.50, confidence=0.80),
        PeerStateObservation(peer_id="peer-1", congestion_score=0.50, confidence=0.80),
    ] + [
        PeerStateObservation(peer_id=f"peer-{i}", congestion_score=0.50, confidence=0.80)
        for i in range(2, 80)
    ]

    snap = _sample_snapshot()
    payload = build_regional_state_payload(snap, local_eval, peers)

    assert payload["scope"] == "regional"
    assert payload["node_id"] == "node-local"
    assert "node-local" not in payload["contributing_peers"]
    assert len(payload["contributing_peers"]) == 64
    assert payload["contributing_peers"][0] == "peer-1"
