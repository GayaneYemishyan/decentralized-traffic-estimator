from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, TYPE_CHECKING

from inference.ontology import (
    LaneObservation,
    NodeMetricsSnapshot,
    Scope,
    SpatialCell,
    TemporalWindow,
)
from inference.rules import DEFAULT_RULE_CONFIG, RuleConfig, RuleEvaluation

if TYPE_CHECKING:
    from inference.smoother import TemporalSmoother

_DEFAULT_SMOOTHER: Optional["TemporalSmoother"] = None


def format_utc_millis(dt: datetime) -> str:
    """Format a datetime as RFC 3339 UTC with exact millisecond precision."""
    utc_dt = dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    millis = utc_dt.microsecond // 1000
    base = utc_dt.strftime("%Y-%m-%dT%H:%M:%S")
    return f"{base}.{millis:03d}Z"


def _parse_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        normalized = value.replace("Z", "+00:00") if value.endswith("Z") else value
        return datetime.fromisoformat(normalized)
    raise ValueError(f"Unsupported timestamp value: {value!r}")


def _parse_spatial_cell(raw: Any) -> Optional[SpatialCell]:
    if not isinstance(raw, dict):
        return None
    cell_id = raw.get("cell_id")
    if not cell_id:
        return None
    return SpatialCell(
        cell_id=str(cell_id),
        grid_x=int(raw.get("grid_x", 0)),
        grid_y=int(raw.get("grid_y", 0)),
    )


def snapshot_from_metrics_dict(metrics_dict: Dict[str, Any]) -> NodeMetricsSnapshot:
    """Convert a raw metrics payload dict into a NodeMetricsSnapshot."""
    window_raw = metrics_dict.get("window") or {}
    duration = float(
        window_raw.get("duration_seconds", metrics_dict.get("duration_seconds", 1.0))
    )

    if "start" in window_raw and "end" in window_raw:
        start_dt = _parse_dt(window_raw["start"])
        end_dt = _parse_dt(window_raw["end"])
    elif "interval_start" in metrics_dict and "interval_end" in metrics_dict:
        start_dt = _parse_dt(metrics_dict["interval_start"])
        end_dt = _parse_dt(metrics_dict["interval_end"])
    elif "interval_end" in metrics_dict:
        end_dt = _parse_dt(metrics_dict["interval_end"])
        start_dt = datetime.fromtimestamp(
            end_dt.timestamp() - max(duration, 0.0), tz=timezone.utc
        )
    elif "interval_start" in metrics_dict:
        start_dt = _parse_dt(metrics_dict["interval_start"])
        end_dt = datetime.fromtimestamp(
            start_dt.timestamp() + max(duration, 0.0), tz=timezone.utc
        )
    elif "timestamp" in metrics_dict:
        ts_dt = _parse_dt(metrics_dict["timestamp"])
        start_dt = datetime.fromtimestamp(
            ts_dt.timestamp() - max(duration, 0.0), tz=timezone.utc
        )
        end_dt = ts_dt
    else:
        raise KeyError(
            "Metrics payload must include 'interval_start'/'interval_end', 'window', or 'timestamp'"
        )

    lanes: List[LaneObservation] = []
    for item in metrics_dict.get("lanes", []):
        counts = item.get("counts_by_class") or {}
        lanes.append(
            LaneObservation(
                lane_id=str(item["lane_id"]),
                vehicle_count=int(item.get("vehicle_count", sum(counts.values()) if counts else 0)),
                flow_rate_vph=float(item.get("flow_rate_vph", 0.0)),
                avg_occupancy=float(item.get("avg_occupancy", 0.0)),
                counts_by_class={str(k): int(v) for k, v in counts.items()},
                spatial_cell=_parse_spatial_cell(item.get("spatial_cell")),
            )
        )

    return NodeMetricsSnapshot(
        schema_version=str(metrics_dict.get("schema_version", "0.2.0")),
        node_id=str(metrics_dict["node_id"]),
        seq=int(metrics_dict.get("seq", 0)),
        window=TemporalWindow(start=start_dt, end=end_dt),
        source=str(metrics_dict.get("source", "synthetic")),
        lanes=lanes,
        spatial_cell=_parse_spatial_cell(metrics_dict.get("spatial_cell")),
    )


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


def process_metrics(
    metrics_dict: Dict[str, Any],
    smoother: Optional["TemporalSmoother"] = None,
    capacity_vph: float = 2000.0,
    config: RuleConfig = DEFAULT_RULE_CONFIG,
    scope: Scope = Scope.LOCAL,
    schema_version: Optional[str] = None,
) -> Dict[str, object]:
    """Validate/convert metrics_dict, run temporal smoothing, and build a state_dict."""
    from inference.smoother import TemporalSmoother

    global _DEFAULT_SMOOTHER
    active_smoother = smoother
    if active_smoother is None:
        if _DEFAULT_SMOOTHER is None:
            _DEFAULT_SMOOTHER = TemporalSmoother(
                capacity_vph=capacity_vph,
                rule_config=config,
            )
        active_smoother = _DEFAULT_SMOOTHER

    snapshot = snapshot_from_metrics_dict(metrics_dict)
    evaluation = active_smoother.update(snapshot)
    return build_state_payload(
        snapshot=snapshot,
        evaluation=evaluation,
        scope=scope,
        schema_version=schema_version,
    )
