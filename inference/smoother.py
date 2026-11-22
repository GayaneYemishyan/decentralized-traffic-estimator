from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict

from inference.ontology import NodeMetricsSnapshot, TrafficState
from inference.rules import (
    DEFAULT_RULE_CONFIG,
    RuleConfig,
    RuleEvaluation,
    _clamp01,
    _ensure_finite,
    classify_state,
    evaluate_snapshot,
)


@dataclass
class TemporalSmoother:
    """EMA smoother keyed by node_id with optional hysteresis and minimum dwell steps."""

    alpha: float = 0.5
    capacity_vph: float = 2000.0
    hysteresis: float = 0.0
    min_dwell_steps: int = 1
    rule_config: RuleConfig = DEFAULT_RULE_CONFIG
    _last_score: Dict[str, float] = field(default_factory=dict)
    _last_confidence: Dict[str, float] = field(default_factory=dict)
    _last_state: Dict[str, TrafficState] = field(default_factory=dict)
    _dwell_count: Dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _ensure_finite(self.alpha, "alpha")
        _ensure_finite(self.capacity_vph, "capacity_vph")
        hyst = _ensure_finite(self.hysteresis, "hysteresis")
        if hyst < 0.0 or hyst >= 0.5:
            raise ValueError("hysteresis must be in [0.0, 0.5)")
        if self.min_dwell_steps < 1:
            raise ValueError("min_dwell_steps must be >= 1")
        if not math.isfinite(self.min_dwell_steps):
            raise ValueError("min_dwell_steps must be finite")

    def _next_state_with_hysteresis(self, node: str, score: float) -> TrafficState:
        candidate = classify_state(score, config=self.rule_config)
        prev_state = self._last_state.get(node)
        if prev_state is None:
            self._last_state[node] = candidate
            self._dwell_count[node] = 1
            return candidate

        if candidate == prev_state:
            self._dwell_count[node] = self._dwell_count.get(node, 0) + 1
            return prev_state

        # Enforce minimum dwell steps before allowing a state transition.
        if self._dwell_count.get(node, 1) < self.min_dwell_steps:
            self._dwell_count[node] = self._dwell_count.get(node, 0) + 1
            return prev_state

        # Enforce hysteresis margin around threshold boundaries.
        mod_t = self.rule_config.moderate_threshold
        cong_t = self.rule_config.congested_threshold
        h = self.hysteresis

        allowed = False
        if prev_state == TrafficState.FREE:
            # Must move clearly above moderate_threshold to enter MODERATE/CONGESTED
            allowed = score >= (mod_t + h)
        elif prev_state == TrafficState.MODERATE:
            if candidate == TrafficState.FREE:
                allowed = score <= (mod_t - h)
            elif candidate == TrafficState.CONGESTED:
                allowed = score >= (cong_t + h)
        elif prev_state == TrafficState.CONGESTED:
            # Must drop clearly below congested_threshold to step down
            allowed = score <= (cong_t - h)

        if allowed:
            self._last_state[node] = candidate
            self._dwell_count[node] = 1
            return candidate

        self._dwell_count[node] = self._dwell_count.get(node, 0) + 1
        return prev_state

    def update(self, snapshot: NodeMetricsSnapshot) -> RuleEvaluation:
        raw = evaluate_snapshot(
            snapshot,
            capacity_vph=self.capacity_vph,
            config=self.rule_config,
        )
        node = snapshot.node_id

        if node not in self._last_score:
            smoothed_score = _clamp01(raw.congestion_score, "congestion_score")
            smoothed_conf = _clamp01(raw.confidence, "confidence")
        else:
            a = _clamp01(self.alpha, "alpha")
            prev_score = _clamp01(self._last_score[node], "last_score")
            prev_conf = _clamp01(self._last_confidence[node], "last_confidence")
            smoothed_score = round(
                _clamp01(a * raw.congestion_score + (1.0 - a) * prev_score, "smoothed_score"),
                4,
            )
            smoothed_conf = round(
                _clamp01(a * raw.confidence + (1.0 - a) * prev_conf, "smoothed_confidence"),
                4,
            )

        self._last_score[node] = smoothed_score
        self._last_confidence[node] = smoothed_conf
        state = self._next_state_with_hysteresis(node, smoothed_score)

        return RuleEvaluation(
            congestion_score=smoothed_score,
            confidence=smoothed_conf,
            state=state,
        )