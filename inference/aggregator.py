from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from inference.ontology import NodeMetricsSnapshot, Scope
from inference.rules import (
    DEFAULT_RULE_CONFIG,
    RuleConfig,
    RuleEvaluation,
    _clamp01,
    _ensure_finite,
    classify_state,
)
from inference.state_builder import build_state_payload


@dataclass(frozen=True)
class PeerStateObservation:
    """Incoming state summary from a neighboring/peer node."""

    peer_id: str
    congestion_score: float
    confidence: float
    seq: Optional[int] = None
    created_at: Optional[datetime] = None
    received_at: Optional[datetime] = None
    timestamp: Optional[datetime] = None

    def effective_created_at(self) -> Optional[datetime]:
        return self.created_at if self.created_at is not None else self.timestamp

    def effective_received_at(self) -> Optional[datetime]:
        if self.received_at is not None:
            return self.received_at
        if self.timestamp is not None:
            return self.timestamp
        return self.created_at


@dataclass(frozen=True)
class AggregatedRegionalState:
    """Result of combining local evaluation with peer state observations."""

    evaluation: RuleEvaluation
    contributing_peers: List[str]
    peer_count: int


def _collect_unique_peers(peers: Iterable[str], max_peers: int = 64) -> List[str]:
    seen = set()
    ordered: List[str] = []
    for peer_id in peers:
        if not peer_id or peer_id in seen:
            continue
        seen.add(peer_id)
        ordered.append(peer_id)
        if len(ordered) >= max_peers:
            break
    return ordered


def _to_utc_timestamp(dt: datetime) -> float:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc).timestamp()
    return dt.astimezone(timezone.utc).timestamp()


def _is_stale(
    obs: PeerStateObservation,
    max_age_s: Optional[float],
    reference_time: Optional[datetime],
) -> bool:
    if max_age_s is None:
        return False
    max_age = _ensure_finite(max_age_s, "max_age_s")
    if max_age < 0.0:
        raise ValueError("max_age_s must be non-negative")
    recv_dt = obs.effective_received_at()
    if recv_dt is None:
        return False
    ref_dt = reference_time if reference_time is not None else datetime.now(timezone.utc)
    age_s = _to_utc_timestamp(ref_dt) - _to_utc_timestamp(recv_dt)
    _ensure_finite(age_s, "observation_age_s")
    return age_s > max_age


def _obs_rank(obs: PeerStateObservation) -> Tuple[float, int]:
    created_dt = obs.effective_created_at()
    created_ts = _to_utc_timestamp(created_dt) if created_dt is not None else float("-inf")
    seq_val = obs.seq if obs.seq is not None else -1
    return (created_ts, seq_val)


def aggregate_peer_states(
    local_evaluation: RuleEvaluation,
    peer_states: Sequence[PeerStateObservation],
    local_node_id: Optional[str] = None,
    max_peers: int = 64,
    max_age_s: Optional[float] = None,
    reference_time: Optional[datetime] = None,
    config: RuleConfig = DEFAULT_RULE_CONFIG,
) -> AggregatedRegionalState:
    """Aggregate local evaluation with peer states into a regional score and confidence."""
    _ensure_finite(local_evaluation.congestion_score, "local_evaluation.congestion_score")
    _ensure_finite(local_evaluation.confidence, "local_evaluation.confidence")

    best_by_peer: Dict[str, PeerStateObservation] = {}
    peer_order: List[str] = []

    for obs in peer_states:
        if not obs.peer_id or obs.peer_id == local_node_id:
            continue
        score = _clamp01(obs.congestion_score, "congestion_score")
        conf = _clamp01(obs.confidence, "confidence")
        if _is_stale(obs, max_age_s=max_age_s, reference_time=reference_time):
            continue

        normalized = PeerStateObservation(
            peer_id=obs.peer_id,
            congestion_score=score,
            confidence=conf,
            seq=obs.seq,
            created_at=obs.created_at,
            received_at=obs.received_at,
            timestamp=obs.timestamp,
        )

        existing = best_by_peer.get(obs.peer_id)
        if existing is None:
            best_by_peer[obs.peer_id] = normalized
            peer_order.append(obs.peer_id)
        elif _obs_rank(normalized) > _obs_rank(existing):
            best_by_peer[obs.peer_id] = normalized

    contributing_peers = _collect_unique_peers(peer_order, max_peers=max_peers)
    valid_peers: List[PeerStateObservation] = [
        best_by_peer[peer_id] for peer_id in contributing_peers
    ]

    if not valid_peers:
        return AggregatedRegionalState(
            evaluation=RuleEvaluation(
                congestion_score=local_evaluation.congestion_score,
                confidence=local_evaluation.confidence,
                state=local_evaluation.state,
            ),
            contributing_peers=[],
            peer_count=0,
        )

    entries: List[Tuple[float, float]] = [
        (
            _clamp01(local_evaluation.congestion_score, "local_score"),
            max(0.05, _clamp01(local_evaluation.confidence, "local_confidence")),
        )
    ]
    for peer in valid_peers:
        entries.append((peer.congestion_score, max(0.05, peer.confidence)))

    total_weight = sum(weight for _, weight in entries)
    weighted_score = sum(score * weight for score, weight in entries) / total_weight
    regional_score = round(_clamp01(weighted_score, "regional_score"), 4)

    all_scores = [local_evaluation.congestion_score] + [p.congestion_score for p in valid_peers]
    mean_score = sum(all_scores) / len(all_scores)
    mean_abs_dev = sum(abs(s - mean_score) for s in all_scores) / len(all_scores)
    agreement = _clamp01(1.0 - min(1.0, mean_abs_dev * 2.0), "agreement")

    coverage_bonus = 0.15 * min(1.0, len(valid_peers) / 4.0)

    mean_conf = sum(weight for _, weight in entries) / len(entries)
    regional_confidence = round(
        _clamp01(0.75 * mean_conf + 0.25 * agreement + coverage_bonus, "regional_confidence"),
        4,
    )

    regional_state = classify_state(regional_score, config=config)

    return AggregatedRegionalState(
        evaluation=RuleEvaluation(
            congestion_score=regional_score,
            confidence=regional_confidence,
            state=regional_state,
        ),
        contributing_peers=contributing_peers,
        peer_count=len(valid_peers),
    )


def build_regional_state_payload(
    snapshot: NodeMetricsSnapshot,
    local_evaluation: RuleEvaluation,
    peer_states: Sequence[PeerStateObservation],
    schema_version: Optional[str] = None,
    max_age_s: Optional[float] = None,
    reference_time: Optional[datetime] = None,
    config: RuleConfig = DEFAULT_RULE_CONFIG,
) -> Dict[str, object]:
    """Build a state_schema.json payload with scope='regional' and aggregated metrics."""
    aggregated = aggregate_peer_states(
        local_evaluation=local_evaluation,
        peer_states=peer_states,
        local_node_id=snapshot.node_id,
        max_age_s=max_age_s,
        reference_time=reference_time,
        config=config,
    )
    return build_state_payload(
        snapshot=snapshot,
        evaluation=aggregated.evaluation,
        scope=Scope.REGIONAL,
        contributing_peers=aggregated.contributing_peers,
        schema_version=schema_version,
    )