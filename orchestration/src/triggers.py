"""Threshold triggers and automated actions (P4).

A small, deterministic state machine over *state messages*:

    IDLE --(score >= enter_score AND confidence >= min_confidence,
            enter_consecutive times in a row)--> ACTIVE   emits congestion_alert
    ACTIVE --(score < exit_score, exit_consecutive times in a row)--> IDLE
                                                           emits congestion_cleared

Design decisions (all configurable, all boundary-tested):
  * Compare ``congestion_score`` (as the contract says), never the label.
  * Enter is inclusive (>=), exit is strict (<). exit_score < enter_score gives a
    hysteresis band in which an active alert neither clears nor re-fires.
  * Low-confidence readings never *start* an alert. Clearing looks at the score
    only, so an alert cannot get stuck because confidence dipped.
  * After a clear, a new alert is suppressed for ``cooldown_s`` (anti-flapping).
  * Engine state is in memory: after a node restart a jam in progress alerts again.
"""
from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

log = logging.getLogger(__name__)

ALERT = "congestion_alert"
CLEARED = "congestion_cleared"
_SCOPES = ("local", "regional")


def _finite(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number, got {value!r}")
    return float(value)


def _positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be an integer >= 1, got {value!r}")
    return value


@dataclass(frozen=True)
class TriggerConfig:
    """Thresholds for the alert state machine."""

    enter_score: float = 0.80
    exit_score: float = 0.70
    min_confidence: float = 0.65
    enter_consecutive: int = 2
    exit_consecutive: int = 2
    cooldown_s: float = 30.0
    scope: str = "regional"  # which state scope is evaluated

    def __post_init__(self) -> None:
        enter = _finite(self.enter_score, "enter_score")
        exit_ = _finite(self.exit_score, "exit_score")
        conf = _finite(self.min_confidence, "min_confidence")
        cooldown = _finite(self.cooldown_s, "cooldown_s")
        if not 0.0 <= exit_ <= enter <= 1.0:
            raise ValueError("need 0 <= exit_score <= enter_score <= 1")
        if not 0.0 <= conf <= 1.0:
            raise ValueError("min_confidence must be within [0, 1]")
        if cooldown < 0.0:
            raise ValueError("cooldown_s must be >= 0")
        _positive_int(self.enter_consecutive, "enter_consecutive")
        _positive_int(self.exit_consecutive, "exit_consecutive")
        if self.scope not in _SCOPES:
            raise ValueError(f"scope must be one of {_SCOPES}")


@dataclass(frozen=True)
class Action:
    """An automated action decided by the engine."""

    kind: str
    node_id: str
    seq: int
    state_timestamp: str
    congestion_score: float
    confidence: float
    scope: str
    reason: str

    def to_payload(self) -> dict[str, Any]:
        """Free-form ledger payload (entry_type 'action')."""
        return {
            "kind": self.kind,
            "node_id": self.node_id,
            "seq": self.seq,
            "state_timestamp": self.state_timestamp,
            "congestion_score": self.congestion_score,
            "confidence": self.confidence,
            "scope": self.scope,
            "reason": self.reason,
        }


def _read_state(state: Mapping[str, Any]) -> tuple[str, str, int, str, float, float]:
    """Pull the fields the engine needs. Raises ValueError on anything unusable."""
    try:
        scope, node_id, seq, ts = state["scope"], state["node_id"], state["seq"], state["timestamp"]
        score = _finite(state["congestion_score"], "congestion_score")
        conf = _finite(state["confidence"], "confidence")
    except KeyError as exc:
        raise ValueError(f"state is missing field {exc}") from exc
    if not (0.0 <= score <= 1.0 and 0.0 <= conf <= 1.0):
        raise ValueError(f"score/confidence out of range: {score}, {conf}")
    return scope, node_id, seq, ts, score, conf


class TriggerEngine:
    """Feed it validated state messages in order; it returns the actions to take."""

    def __init__(self, config: TriggerConfig | None = None, clock: Callable[[], float] = time.monotonic) -> None:
        self.config = config or TriggerConfig()
        self._clock = clock
        self.active = False
        self.suppressed = 0  # alerts held back by the cooldown (for stats)
        self._enter_streak = 0
        self._exit_streak = 0
        self._last_cleared: float | None = None

    def evaluate(self, state: Mapping[str, Any]) -> list[Action]:
        cfg = self.config
        scope, node_id, seq, ts, score, conf = _read_state(state)
        if scope != cfg.scope:
            return []

        def make(kind: str, reason: str) -> Action:
            return Action(kind, node_id, seq, ts, score, conf, scope, reason)

        if not self.active:
            if score >= cfg.enter_score and conf >= cfg.min_confidence:
                self._enter_streak += 1
            else:
                self._enter_streak = 0
                return []
            if self._enter_streak < cfg.enter_consecutive:
                return []
            now = self._clock()
            if self._last_cleared is not None and now - self._last_cleared < cfg.cooldown_s:
                self.suppressed += 1
                return []
            self.active = True
            self._enter_streak = self._exit_streak = 0
            return [make(ALERT, f"score {score:.4f} >= {cfg.enter_score} and confidence {conf:.4f} >= "
                                f"{cfg.min_confidence} for {cfg.enter_consecutive} consecutive state(s)")]

        self._exit_streak = self._exit_streak + 1 if score < cfg.exit_score else 0
        if self._exit_streak < cfg.exit_consecutive:
            return []
        self.active = False
        self._exit_streak = self._enter_streak = 0
        self._last_cleared = self._clock()
        return [make(CLEARED, f"score {score:.4f} < {cfg.exit_score} for {cfg.exit_consecutive} consecutive state(s)")]


# --------------------------------------------------------------------------- handlers
ActionHandler = Callable[[Action], None]


def log_handler(action: Action) -> None:
    log.warning("ACTION %s node=%s score=%.4f confidence=%.4f (%s)",
                action.kind, action.node_id, action.congestion_score, action.confidence, action.reason)


class JsonlActionSink:
    """Appends one JSON line per action: the hook for a signal controller / dashboard on the same host."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def __call__(self, action: Action) -> None:
        line = json.dumps(action.to_payload(), sort_keys=True, separators=(",", ":"), allow_nan=False)
        with self._lock, self._path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()
            os.fsync(fh.fileno())


def dispatch(action: Action, handlers: Iterable[ActionHandler]) -> int:
    """Run every handler; one failing handler never blocks the others. Returns the failure count."""
    failures = 0
    for handler in handlers:
        try:
            handler(action)
        except Exception:
            failures += 1
            log.exception("action handler %r failed for %s", handler, action.kind)
    return failures