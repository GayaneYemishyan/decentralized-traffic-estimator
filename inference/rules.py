from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

from inference.ontology import LaneObservation, NodeMetricsSnapshot, TrafficState


def _ensure_finite(value: float, name: str = "value") -> float:
    val = float(value)
    if not math.isfinite(val):
        raise ValueError(f"{name} must be a finite number, got {value!r}")
    return val


@dataclass(frozen=True)
class RuleConfig:
    """Configurable thresholds and scoring weights for traffic classification."""

    moderate_threshold: float = 0.35
    congested_threshold: float = 0.70
    occupancy_weight: float = 0.65
    flow_weight: float = 0.35

    def __post_init__(self) -> None:
        _ensure_finite(self.moderate_threshold, "moderate_threshold")
        _ensure_finite(self.congested_threshold, "congested_threshold")
        _ensure_finite(self.occupancy_weight, "occupancy_weight")
        _ensure_finite(self.flow_weight, "flow_weight")
        if self.moderate_threshold < 0.0 or self.congested_threshold > 1.0:
            raise ValueError("thresholds must lie within [0.0, 1.0]")
        if self.moderate_threshold > self.congested_threshold:
            raise ValueError("moderate_threshold must not exceed congested_threshold")
        if self.occupancy_weight < 0.0 or self.flow_weight < 0.0:
            raise ValueError("weights must be non-negative")
        if (self.occupancy_weight + self.flow_weight) <= 0.0:
            raise ValueError("sum of occupancy_weight and flow_weight must be positive")


DEFAULT_RULE_CONFIG = RuleConfig()


@dataclass(frozen=True)
class RuleEvaluation:
    congestion_score: float
    confidence: float
    state: TrafficState


def _clamp01(value: float, name: str = "value") -> float:
    val = _ensure_finite(value, name)
    if val < 0.0:
        return 0.0
    if val > 1.0:
        return 1.0
    return val


def score_lane(
    lane: LaneObservation,
    capacity_vph: float = 2000.0,
    config: RuleConfig = DEFAULT_RULE_CONFIG,
) -> float:
    cap = _ensure_finite(capacity_vph, "capacity_vph")
    occ = _ensure_finite(lane.avg_occupancy, "avg_occupancy")
    flow = _ensure_finite(lane.flow_rate_vph, "flow_rate_vph")

    occupancy_component = _clamp01(occ, "avg_occupancy")
    flow_component = _clamp01(flow / cap, "flow_rate_vph") if cap > 0 else 0.0

    total_w = config.occupancy_weight + config.flow_weight
    w_occ = config.occupancy_weight / total_w
    w_flow = config.flow_weight / total_w
    return _clamp01(w_occ * occupancy_component + w_flow * flow_component, "lane_score")


def classify_state(
    congestion_score: float,
    config: RuleConfig = DEFAULT_RULE_CONFIG,
) -> TrafficState:
    score = _clamp01(congestion_score, "congestion_score")
    if score < config.moderate_threshold:
        return TrafficState.FREE
    if score < config.congested_threshold:
        return TrafficState.MODERATE
    return TrafficState.CONGESTED


def compute_confidence(lanes: Sequence[LaneObservation], window_seconds: float) -> float:
    win_sec = _ensure_finite(window_seconds, "window_seconds")
    if not lanes:
        return 0.0
    for lane in lanes:
        _ensure_finite(lane.avg_occupancy, "avg_occupancy")
        _ensure_finite(lane.flow_rate_vph, "flow_rate_vph")

    lane_factor = _clamp01(len(lanes) / 4.0, "lane_factor")
    total_vehicles = sum(max(lane.vehicle_count, 0) for lane in lanes)
    volume_factor = _clamp01(total_vehicles / 10.0, "volume_factor")
    window_factor = 1.0 if win_sec > 0 else 0.3
    return round(
        _clamp01(0.45 * lane_factor + 0.35 * volume_factor + 0.20 * window_factor, "confidence"),
        4,
    )


def evaluate_snapshot(
    snapshot: NodeMetricsSnapshot,
    capacity_vph: float = 2000.0,
    config: RuleConfig = DEFAULT_RULE_CONFIG,
) -> RuleEvaluation:
    _ensure_finite(capacity_vph, "capacity_vph")
    if not snapshot.lanes:
        return RuleEvaluation(congestion_score=0.0, confidence=0.0, state=TrafficState.FREE)

    lane_scores = [
        score_lane(lane, capacity_vph=capacity_vph, config=config) for lane in snapshot.lanes
    ]
    avg_score = round(_clamp01(sum(lane_scores) / len(lane_scores), "avg_score"), 4)
    confidence = compute_confidence(snapshot.lanes, snapshot.window.duration_seconds)
    state = classify_state(avg_score, config=config)
    return RuleEvaluation(congestion_score=avg_score, confidence=confidence, state=state)