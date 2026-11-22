from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from inference.ontology import NodeMetricsSnapshot, Scope, TrafficState
from inference.rules import RuleEvaluation, _clamp01, classify_state
from inference.state_builder import build_state_payload


@dataclass(frozen=True)
class PeerStateObservation:
    """Incoming state summary from a neighboring/peer node."""

    peer_id: str
    congestion_score: float
    confidence: float
    seq: Optional[int] = None


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


def aggregate_peer_states(
    local_evaluation: RuleEvaluation,
    peer_states: Sequence[PeerStateObservation],
    local_node_id: Optional[str] = None,
    max_peers: int = 64,
) -> AggregatedRegionalState:
    """Aggregate local evaluation with peer states into a regional score and confidence."""
    valid_peers: List[PeerStateObservation] = []
    seen_ids = set()

    for obs in peer_states:
        if not obs.peer_id or obs.peer_id == local_node_id or obs.peer_id in seen_ids:
            continue
        seen_ids.add(obs.peer_id)
        valid_peers.append(
            PeerStateObservation(
                peer_id=obs.peer_id,
                congestion_score=_clamp01(float(obs.congestion_score)),
                confidence=_clamp01(float(obs.confidence)),
                seq=obs.seq,
            )
        )
        if len(valid_peers) >= max_peers:
            break

    contributing_peers = _collect_unique_peers(
        (p.peer_id for p in valid_peers), max_peers=max_peers
    )

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

    # Combine local node (weight >= 0.05) and peers weighted by their confidence.
    entries: List[Tuple[float, float]] = [
        (_clamp01(local_evaluation.congestion_score), max(0.05, _clamp01(local_evaluation.confidence)))
    ]
    for peer in valid_peers:
        entries.append((peer.congestion_score, max(0.05, peer.confidence)))

    total_weight = sum(weight for _, weight in entries)
    weighted_score = sum(score * weight for score, weight in entries) / total_weight
    regional_score = round(_clamp01(weighted_score), 4)

    # Agreement factor: 1.0 when all scores match, lower when scores diverge strongly.
    all_scores = [local_evaluation.congestion_score] + [p.congestion_score for p in valid_peers]
    mean_score = sum(all_scores) / len(all_scores)
    mean_abs_dev = sum(abs(s - mean_score) for s in all_scores) / len(all_scores)
    agreement = _clamp01(1.0 - min(1.0, mean_abs_dev * 2.0))

    # Coverage bonus up to +0.15 as more peers contribute (saturates around 4 peers).
    coverage_bonus = 0.15 * min(1.0, len(valid_peers) / 4.0)

    mean_conf = sum(weight for _, weight in entries) / len(entries)
    regional_confidence = round(
        _clamp01(0.75 * mean_conf + 0.25 * agreement + coverage_bonus),
        4,
    )

    regional_state = classify_state(regional_score)

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
) -> Dict[str, object]:
    """Build a state_schema.json payload with scope='regional' and aggregated metrics."""
    aggregated = aggregate_peer_states(
        local_evaluation=local_evaluation,
        peer_states=peer_states,
        local_node_id=snapshot.node_id,
    )
    return build_state_payload(
        snapshot=snapshot,
        evaluation=aggregated.evaluation,
        scope=Scope.REGIONAL,
        contributing_peers=aggregated.contributing_peers,
        schema_version=schema_version,
    )
