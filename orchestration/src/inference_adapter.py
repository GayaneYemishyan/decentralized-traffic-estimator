"""Adapter between orchestration and P2's ``inference`` package.

Keeps the microservice independent of P2's internals: only this file knows
Lilit's function names. Tests for the microservice use a fake adapter.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Protocol, Sequence

from .serializer import SUPPORTED_SCHEMA_VERSION


@dataclass(frozen=True)
class PeerObservation:
    """Latest local-scope state received from one peer."""

    peer_id: str
    congestion_score: float
    confidence: float
    seq: int
    created_at: datetime   # peer's clock: ordering only
    received_at: datetime  # our clock: freshness only


class InferenceUnavailableError(RuntimeError):
    """The P2 package cannot be imported."""


class InferenceAdapter(Protocol):
    def local_state(self, metrics: Mapping[str, Any]) -> dict[str, Any]:
        """Validated metrics -> state message with scope 'local' (smoothing inside)."""

    def regional_state(
        self,
        metrics: Mapping[str, Any],
        local_state: Mapping[str, Any],
        peers: Sequence[PeerObservation],
        *,
        now: datetime,
    ) -> dict[str, Any]:
        """Local state + fresh peers -> state message with scope 'regional'."""


class LilitInference:
    """Default adapter over ``inference`` (PR #58)."""

    def __init__(self, *, peer_max_age_s: float | None = 30.0, smoother: Any | None = None) -> None:
        try:
            import inference as inf
        except ImportError as exc:
            raise InferenceUnavailableError(
                "cannot import the 'inference' package (P2); put its parent folder on PYTHONPATH"
            ) from exc
        self._inf = inf
        self._smoother = smoother or inf.TemporalSmoother()
        self._max_age_s = peer_max_age_s

    @staticmethod
    def _prepare(metrics: Mapping[str, Any]) -> dict[str, Any]:
        # TEMPORARY SHIM: inference.snapshot_from_metrics_dict reads "window"/"timestamp"
        # but metrics 0.2.0 carries interval_start/interval_end. Remove once P2 fixes it.
        prepared = dict(metrics)
        prepared["window"] = {"start": metrics["interval_start"], "end": metrics["interval_end"]}
        return prepared

    def local_state(self, metrics: Mapping[str, Any]) -> dict[str, Any]:
        return dict(self._inf.process_metrics(
            self._prepare(metrics),
            smoother=self._smoother,
            scope=self._inf.Scope.LOCAL,
            schema_version=SUPPORTED_SCHEMA_VERSION,
        ))

    def regional_state(self, metrics, local_state, peers, *, now):
        inf = self._inf
        snapshot = inf.snapshot_from_metrics_dict(self._prepare(metrics))
        local_eval = inf.RuleEvaluation(
            congestion_score=local_state["congestion_score"],
            confidence=local_state["confidence"],
            state=inf.TrafficState(local_state["state"]),
        )
        observations = [
            inf.PeerStateObservation(
                peer_id=p.peer_id,
                congestion_score=p.congestion_score,
                confidence=p.confidence,
                seq=p.seq,
                created_at=p.created_at,
                received_at=p.received_at,
            )
            for p in peers
        ]
        return dict(inf.build_regional_state_payload(
            snapshot,
            local_eval,
            observations,
            schema_version=SUPPORTED_SCHEMA_VERSION,
            max_age_s=self._max_age_s,
            reference_time=now,
        ))