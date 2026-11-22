from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from inference.ontology import LaneObservation, NodeMetricsSnapshot, TrafficState


@dataclass(frozen=True)
class RuleEvaluation:
    congestion_score: float
    confidence: float
    state: TrafficState


def _clamp01(value: float) -> float:
    if value < 0.0:
        return 0.0
    if value > 1.0:
        return 1.0
    return value


def score_lane(lane: LaneObservation, capacity_vph: float = 2000.0) -> float:
    occupancy_component = _clamp01(lane.avg_occupancy)
    flow_component = _clamp01(lane.flow_rate_vph / capacity_vph) if capacity_vph > 0 else 0.0
    return _clamp01(0.65 * occupancy_component + 0.35 * flow_component)


def classify_state(congestion_score: float) -> TrafficState:
    score = _clamp01(congestion_score)
    if score < 0.35:
        return TrafficState.FREE
    if score < 0.70:
        return TrafficState.MODERATE
    return TrafficState.CONGESTED


def compute_confidence(lanes: Sequence[LaneObservation], window_seconds: float) -> float:
    if not lanes:
        return 0.0
    lane_factor = _clamp01(len(lanes) / 4.0)
    total_vehicles = sum(max(lane.vehicle_count, 0) for lane in lanes)
    volume_factor = _clamp01(total_vehicles / 10.0)
    window_factor = 1.0 if window_seconds > 0 else 0.3
    return round(_clamp01(0.45 * lane_factor + 0.35 * volume_factor + 0.20 * window_factor), 4)


def evaluate_snapshot(snapshot: NodeMetricsSnapshot, capacity_vph: float = 2000.0) -> RuleEvaluation:
    if not snapshot.lanes:
        return RuleEvaluation(congestion_score=0.0, confidence=0.0, state=TrafficState.FREE)

    lane_scores = [score_lane(lane, capacity_vph=capacity_vph) for lane in snapshot.lanes]
    avg_score = round(_clamp01(sum(lane_scores) / len(lane_scores)), 4)
    confidence = compute_confidence(snapshot.lanes, snapshot.window.duration_seconds)
    state = classify_state(avg_score)
    return RuleEvaluation(congestion_score=avg_score, confidence=confidence, state=state)
