from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional

from inference.ontology import NodeMetricsSnapshot, Scope
from inference.rules import RuleEvaluation


def format_utc_millis(dt: datetime) -> str:
    """Format a datetime as RFC 3339 UTC with exact millisecond precision."""
    utc_dt = dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    millis = utc_dt.microsecond // 1000
    base = utc_dt.strftime("%Y-%m-%dT%H:%M:%S")
    return f"{base}.{millis:03d}Z"


def build_state_payload(
    snapshot: NodeMetricsSnapshot,
    evaluation: RuleEvaluation,
    scope: Scope = Scope.LOCAL,
    contributing_peers: Optional[Iterable[str]] = None,
    schema_version: Optional[str] = None,
) -> Dict[str, object]:
    """Build a dictionary conforming to state_schema.json."""
    if scope == Scope.LOCAL:
        peers: List[str] = []
    else:
        seen = set()
        peers = []
        for peer in (contributing_peers or []):
            if peer not in seen:
                seen.add(peer)
                peers.append(peer)
        peers = peers[:64]

    return {
        "schema_version": schema_version or snapshot.schema_version,
        "node_id": snapshot.node_id,
        "seq": snapshot.seq,
        "timestamp": format_utc_millis(snapshot.window.end),
        "state": evaluation.state.value,
        "congestion_score": evaluation.congestion_score,
        "confidence": evaluation.confidence,
        "scope": scope.value,
        "contributing_peers": peers,
    }
