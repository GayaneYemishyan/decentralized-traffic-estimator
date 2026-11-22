from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional


class TrafficState(str, Enum):
    FREE = "free"
    MODERATE = "moderate"
    CONGESTED = "congested"


class Scope(str, Enum):
    LOCAL = "local"
    REGIONAL = "regional"


@dataclass(frozen=True)
class SpatialCell:
    cell_id: str
    grid_x: int = 0
    grid_y: int = 0


@dataclass(frozen=True)
class TemporalWindow:
    start: datetime
    end: datetime

    @property
    def duration_seconds(self) -> float:
        delta = (self.end - self.start).total_seconds()
        return max(delta, 0.0)


@dataclass(frozen=True)
class LaneObservation:
    lane_id: str
    vehicle_count: int
    flow_rate_vph: float
    avg_occupancy: float
    counts_by_class: Dict[str, int] = field(default_factory=dict)
    spatial_cell: Optional[SpatialCell] = None


@dataclass(frozen=True)
class NodeMetricsSnapshot:
    schema_version: str
    node_id: str
    seq: int
    window: TemporalWindow
    source: str
    lanes: List[LaneObservation]
    spatial_cell: Optional[SpatialCell] = None
