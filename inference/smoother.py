from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict

from inference.ontology import NodeMetricsSnapshot
from inference.rules import RuleEvaluation, _clamp01, classify_state, evaluate_snapshot


@dataclass
class TemporalSmoother:
    """Exponential moving average smoother keyed by node_id."""

    alpha: float = 0.5
    capacity_vph: float = 2000.0
    _last_score: Dict[str, float] = field(default_factory=dict)
    _last_confidence: Dict[str, float] = field(default_factory=dict)

    def update(self, snapshot: NodeMetricsSnapshot) -> RuleEvaluation:
        raw = evaluate_snapshot(snapshot, capacity_vph=self.capacity_vph)
        node = snapshot.node_id

        if node not in self._last_score:
            smoothed_score = raw.congestion_score
            smoothed_conf = raw.confidence
        else:
            a = _clamp01(self.alpha)
            smoothed_score = round(
                _clamp01(a * raw.congestion_score + (1.0 - a) * self._last_score[node]),
                4,
            )
            smoothed_conf = round(
                _clamp01(a * raw.confidence + (1.0 - a) * self._last_confidence[node]),
                4,
            )

        self._last_score[node] = smoothed_score
        self._last_confidence[node] = smoothed_conf
        state = classify_state(smoothed_score)

        return RuleEvaluation(
            congestion_score=smoothed_score,
            confidence=smoothed_conf,
            state=state,
        )
