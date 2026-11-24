"""Unit tests for src/triggers.py."""
from __future__ import annotations

import json

import pytest

from src.triggers import ALERT, CLEARED, Action, JsonlActionSink, TriggerConfig, TriggerEngine, dispatch


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def state(score: float, conf: float = 0.9, scope: str = "regional", seq: int = 0) -> dict:
    return {"schema_version": "0.2.0", "node_id": "node-a", "seq": seq, "timestamp": "2026-11-24T12:00:00.000Z",
            "state": "congested", "congestion_score": score, "confidence": conf, "scope": scope,
            "contributing_peers": []}


def engine(**kw):
    kw.setdefault("enter_consecutive", 1)
    kw.setdefault("exit_consecutive", 1)
    kw.setdefault("cooldown_s", 0.0)
    clock = Clock()
    return TriggerEngine(TriggerConfig(**kw), clock=clock), clock


def kinds(actions) -> list[str]:
    return [a.kind for a in actions]


@pytest.mark.parametrize("kwargs", [
    {"enter_score": 1.1}, {"enter_score": float("nan")}, {"enter_score": 0.8, "exit_score": 0.9},
    {"min_confidence": -0.1}, {"enter_consecutive": 0}, {"exit_consecutive": True},
    {"cooldown_s": -1}, {"scope": "global"},
])
def test_config_rejects_bad_values(kwargs):
    with pytest.raises(ValueError):
        TriggerConfig(**kwargs)


def test_threshold_boundary_is_inclusive():
    eng, _ = engine()
    assert eng.evaluate(state(0.7999)) == []
    assert kinds(eng.evaluate(state(0.80))) == [ALERT]


def test_confidence_gate_boundary():
    eng, _ = engine()
    assert eng.evaluate(state(0.95, conf=0.6499)) == []
    assert kinds(eng.evaluate(state(0.95, conf=0.65))) == [ALERT]


def test_requires_consecutive_observations():
    eng, _ = engine(enter_consecutive=2)
    assert eng.evaluate(state(0.9)) == []
    assert eng.evaluate(state(0.5)) == []  # streak broken
    assert eng.evaluate(state(0.9)) == []
    assert kinds(eng.evaluate(state(0.9))) == [ALERT]


def test_no_repeat_alert_while_active():
    eng, _ = engine()
    assert kinds(eng.evaluate(state(0.9))) == [ALERT]
    for _ in range(5):
        assert eng.evaluate(state(0.95)) == []
    assert eng.active


def test_hysteresis_band_keeps_alert_active_and_exit_is_strict():
    eng, _ = engine()
    eng.evaluate(state(0.9))
    assert eng.evaluate(state(0.75)) == [] and eng.active
    assert eng.evaluate(state(0.70)) == [] and eng.active  # exit uses strict <
    assert kinds(eng.evaluate(state(0.6999))) == [CLEARED] and not eng.active


def test_band_resets_exit_streak():
    eng, _ = engine(exit_consecutive=2)
    eng.evaluate(state(0.9))
    assert eng.evaluate(state(0.5)) == []
    assert eng.evaluate(state(0.75)) == []  # back in the band: streak restarts
    assert eng.evaluate(state(0.5)) == []
    assert kinds(eng.evaluate(state(0.5))) == [CLEARED]


def test_low_confidence_does_not_block_clearing():
    eng, _ = engine()
    eng.evaluate(state(0.9))
    assert kinds(eng.evaluate(state(0.1, conf=0.1))) == [CLEARED]


def test_cooldown_suppresses_realert_then_allows():
    eng, clock = engine(cooldown_s=30)
    assert kinds(eng.evaluate(state(0.9))) == [ALERT]
    assert kinds(eng.evaluate(state(0.1))) == [CLEARED]
    assert eng.evaluate(state(0.9)) == [] and eng.suppressed == 1 and not eng.active
    clock.advance(31)
    assert kinds(eng.evaluate(state(0.9))) == [ALERT]


def test_scope_filter():
    eng, _ = engine()
    assert eng.evaluate(state(0.99, scope="local")) == []
    eng_local, _ = engine(scope="local")
    assert kinds(eng_local.evaluate(state(0.99, scope="local"))) == [ALERT]


@pytest.mark.parametrize("bad", [
    {}, {**state(0.9), "congestion_score": float("nan")}, {**state(0.9), "confidence": 1.5},
    {**state(0.9), "congestion_score": True},
])
def test_malformed_state_raises(bad):
    with pytest.raises(ValueError):
        TriggerEngine().evaluate(bad)


def test_action_payload_is_strict_json():
    eng, _ = engine()
    [action] = eng.evaluate(state(0.9, seq=7))
    payload = action.to_payload()
    json.dumps(payload, allow_nan=False)
    assert payload["kind"] == ALERT and payload["seq"] == 7 and payload["node_id"] == "node-a"


def test_jsonl_sink_appends_lines(tmp_path):
    sink = JsonlActionSink(tmp_path / "out" / "actions.jsonl")
    eng, _ = engine()
    sink(eng.evaluate(state(0.9))[0])
    sink(eng.evaluate(state(0.1))[0])
    lines = (tmp_path / "out" / "actions.jsonl").read_text().splitlines()
    assert [json.loads(x)["kind"] for x in lines] == [ALERT, CLEARED]


def test_dispatch_isolates_failing_handlers():
    seen: list[Action] = []

    def boom(_: Action) -> None:
        raise RuntimeError("handler bug")

    eng, _ = engine()
    [action] = eng.evaluate(state(0.9))
    assert dispatch(action, [boom, seen.append]) == 1
    assert seen == [action]