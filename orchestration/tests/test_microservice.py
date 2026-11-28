"""Unit tests for src/microservice.py: fake bridge, fake inference, real ledger."""
from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone

import pytest
from fake_bridge import FakeBridge

from src import serializer as s
from src.bridge_client import BridgeClient, BridgeMessage, CircuitBreaker
from src.inference_adapter import PeerObservation
from src.ledger import Ledger
from src.microservice import Microservice, PeerTable, ServiceConfig, ServiceError, _RateLimiter
from src.triggers import TriggerConfig

NOW = datetime(2026, 11, 24, 12, 0, 0, tzinfo=timezone.utc)
PEER = "12D3KooWFakeOtherPeer00000000000000000000000000"


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


class FakeInference:
    """Score = occupancy of lane 1; regional = mean of local and peers."""

    def local_state(self, metrics):
        occ = metrics["lanes"][0]["avg_occupancy"]
        return {"schema_version": s.SUPPORTED_SCHEMA_VERSION, "node_id": metrics["node_id"], "seq": metrics["seq"],
                "timestamp": metrics["interval_end"], "state": "congested" if occ >= 0.7 else "free",
                "congestion_score": occ, "confidence": 0.9, "scope": "local", "contributing_peers": []}

    def regional_state(self, metrics, local_state, peers, *, now):
        scores = [local_state["congestion_score"]] + [p.congestion_score for p in peers]
        return {**local_state, "scope": "regional", "contributing_peers": [p.peer_id for p in peers],
                "congestion_score": round(sum(scores) / len(scores), 4)}


def metrics_json(occ: float = 0.1, seq: int = 1) -> bytes:
    m = json.loads((s.contracts_dir() / "examples" / "metrics.example.json").read_text())
    m["seq"] = seq
    m["lanes"][0]["avg_occupancy"] = occ
    return json.dumps(m).encode()


def peer_msg(score: float = 0.9, *, node: str = PEER, from_peer: str | None = None, seq: int = 1,
             now: datetime = NOW, scope: str = "local") -> BridgeMessage:
    base = json.loads((s.contracts_dir() / "examples" / "state.example.json").read_text())
    state = {**base, "node_id": node, "seq": seq, "congestion_score": score, "scope": scope,
             "contributing_peers": [] if scope == "local" else ["some-peer"]}
    env = s.build_envelope(state, node, now=now)
    return BridgeMessage(cursor=1, from_peer=from_peer or node, data=s.encode_envelope(env))


@pytest.fixture
def bridge():
    with FakeBridge() as b:
        yield b


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def ledger(tmp_path):
    with Ledger(tmp_path / "ledger.db") as led:
        yield led


@pytest.fixture
def actions() -> list:
    return []


@pytest.fixture
def make_service(bridge, ledger, clock, actions, tmp_path):
    def build(*, with_bridge: bool = True, **cfg):
        cfg.setdefault("trigger", TriggerConfig(enter_consecutive=1, exit_consecutive=1, cooldown_s=0.0))
        cfg.setdefault("poll_timeout_ms", 100)
        client = BridgeClient(bridge.url, publish_attempts=1, backoff_base_s=0.01, backoff_cap_s=0.02,
                              breaker=CircuitBreaker(failure_threshold=50), sleep=lambda _: None) if with_bridge else None
        return Microservice(ServiceConfig(ledger_path=str(tmp_path / "ledger.db"), **cfg), bridge=client,
                            ledger=ledger, inference=FakeInference(), clock=clock, handlers=[actions.append])
    return build


@pytest.fixture
def svc(make_service):
    service = make_service()
    service.resolve_identity(threading.Event())
    return service


def entries(ledger: Ledger, entry_type: str):
    return [e for e in ledger.iter_entries() if e.entry_type == entry_type]


# ----------------------------------------------------------------- identity
def test_node_id_is_the_bridge_peer_id(svc, bridge):
    assert svc.node_id == bridge.peer_id


def test_explicit_node_id_must_match_peer_id(make_service):
    with pytest.raises(ServiceError, match="peer ID"):
        make_service(node_id="node-zzz").resolve_identity(threading.Event())


def test_identity_check_can_be_disabled(make_service, clock):
    service = make_service(node_id="node-zzz", enforce_peer_identity=False)
    assert service.resolve_identity(threading.Event()) == "node-zzz"
    assert service.handle_peer_message(peer_msg(from_peer="12D3KooWSomeoneElse")) == "accepted"


def test_peer_id_change_is_fatal(svc, bridge):
    bridge.peer_id = "12D3KooWChangedPeer0000000000000000000000000000"
    svc._check_identity()
    assert svc.fatal and "peer_id changed" in svc.fatal


# ----------------------------------------------------------------- local pipeline
def test_metrics_become_a_published_recorded_envelope(svc, bridge, ledger):
    result = svc.handle_metrics(metrics_json(0.2))
    assert result and result.published and result.local["node_id"] == bridge.peer_id  # stamped, not "node-a"
    env = s.parse_envelope(bridge.published[0])
    assert env.origin_node_id == bridge.peer_id and env.payload["scope"] == "local"
    local, regional = entries(ledger, "state")
    assert local.message_id == env.message_id and regional.message_id is None
    assert regional.payload["scope"] == "regional"
    assert ledger.verify_chain()


@pytest.mark.parametrize("raw", [b"{not json", b"[]", b'{"schema_version": "0.2.0"}'])
def test_invalid_metrics_are_dropped(svc, bridge, ledger, raw):
    assert svc.handle_metrics(raw) is None
    assert bridge.published == [] and ledger.count() == 0 and svc.stats()["metrics_invalid"] == 1


def test_count_mismatch_is_dropped(svc, bridge):
    m = json.loads(metrics_json())
    m["lanes"][0]["vehicle_count"] = 99
    assert svc.handle_metrics(json.dumps(m).encode()) is None and bridge.published == []


def test_publish_failure_does_not_stop_the_pipeline(svc, bridge, ledger):
    bridge.fail_next(1, 503)
    result = svc.handle_metrics(metrics_json())
    assert result and not result.published and result.regional is not None
    assert svc.stats()["publish_failed"] == 1 and len(entries(ledger, "state")) == 2


def test_inference_failure_drops_metrics_but_loop_survives(svc, bridge, monkeypatch):
    monkeypatch.setattr(svc._inference, "local_state", lambda m: (_ for _ in ()).throw(RuntimeError("bug")))
    assert svc.handle_metrics(metrics_json()) is None and svc.stats()["inference_error"] == 1


def test_alert_and_clear_are_recorded_and_dispatched(svc, ledger, actions):
    assert [a.kind for a in svc.handle_metrics(metrics_json(0.95, seq=1)).actions] == ["congestion_alert"]
    assert [a.kind for a in svc.handle_metrics(metrics_json(0.10, seq=2)).actions] == ["congestion_cleared"]
    assert [a.kind for a in actions] == ["congestion_alert", "congestion_cleared"]
    assert [e.payload["kind"] for e in entries(ledger, "action")] == ["congestion_alert", "congestion_cleared"]
    assert ledger.verify_chain()


def test_no_bridge_mode(make_service, ledger):
    service = make_service(with_bridge=False, node_id="node-local")
    service.resolve_identity()
    result = service.handle_metrics(metrics_json())
    assert result and not result.published and result.local["node_id"] == "node-local"
    assert ledger.count() == 2


# ----------------------------------------------------------------- peer pipeline
def test_peer_state_is_recorded_and_used_in_regional(svc, ledger):
    assert svc.handle_peer_message(peer_msg(0.9)) == "accepted"
    assert len(entries(ledger, "state")) == 1
    result = svc.handle_metrics(metrics_json(0.1))
    assert result.regional["contributing_peers"] == [PEER]
    assert result.regional["congestion_score"] == 0.5  # mean(0.1, 0.9) in the fake


def test_spoofed_origin_is_rejected(svc, ledger):
    assert svc.handle_peer_message(peer_msg(from_peer="12D3KooWSomeoneElse")) == "rejected"
    assert svc.stats()["peer_rejected_IntegrityError"] == 1 and ledger.count() == 0


def test_garbage_from_peer_is_rejected(svc):
    assert svc.handle_peer_message(BridgeMessage(1, PEER, b"{nope")) == "rejected"


def test_duplicate_is_suppressed_in_memory_and_across_restarts(svc, make_service, ledger):
    msg = peer_msg()
    assert svc.handle_peer_message(msg) == "accepted"
    assert svc.handle_peer_message(msg) == "duplicate"
    restarted = make_service()
    restarted.resolve_identity(threading.Event())
    assert restarted.handle_peer_message(msg) == "duplicate"  # the ledger remembers
    assert ledger.count() == 1


def test_regional_states_from_peers_are_not_aggregated(svc, ledger):
    assert svc.handle_peer_message(peer_msg(scope="regional")) == "non_local"
    assert ledger.count() == 0


def test_own_message_is_ignored(svc, bridge):
    assert svc.handle_peer_message(peer_msg(node=bridge.peer_id)) == "self"


def test_future_timestamp_is_rejected(svc):
    assert svc.handle_peer_message(peer_msg(now=NOW + timedelta(minutes=5))) == "rejected"
    assert svc.stats()["peer_rejected_ClockSkewError"] == 1


def test_stale_peer_drops_out_of_regional(svc, clock):
    svc.handle_peer_message(peer_msg(0.9))
    assert svc.handle_metrics(metrics_json(0.1, 1)).regional["contributing_peers"] == [PEER]
    clock.now += timedelta(seconds=31)
    assert svc.handle_metrics(metrics_json(0.1, 2)).regional["contributing_peers"] == []


def test_replayed_old_envelope_is_audited_but_not_used(svc, ledger):
    assert svc.handle_peer_message(peer_msg(now=NOW - timedelta(hours=1))) == "stale"
    assert ledger.count() == 1
    assert svc.handle_metrics(metrics_json()).regional["contributing_peers"] == []


def test_rate_limiter_token_bucket_refills_and_is_per_key():
    t = [0.0]
    rl = _RateLimiter(1.0, 2, clock=lambda: t[0])
    assert rl.allow("a") and rl.allow("a") and not rl.allow("a")
    assert rl.allow("b")
    t[0] = 1.0
    assert rl.allow("a") and not rl.allow("a")


def test_rate_limiter_disabled_and_bounded():
    assert all(_RateLimiter(0, 1).allow("x") for _ in range(100))
    rl = _RateLimiter(1.0, 1, max_keys=2, clock=lambda: 0.0)
    for k in "abc":
        rl.allow(k)
    assert len(rl) == 2


def test_flooding_peer_is_limited_but_others_are_not(make_service):
    service = make_service(peer_rate_per_s=0.001, peer_burst=2)
    service.resolve_identity(threading.Event())
    assert [service.handle_peer_message(peer_msg(0.5)) for _ in range(3)] == ["accepted", "accepted", "rate_limited"]
    other = "12D3KooWAnotherPeer000000000000000000000000000"
    assert service.handle_peer_message(peer_msg(0.5, node=other)) == "accepted"
    assert service.stats()["peer_rate_limited"] == 1


# ----------------------------------------------------------------- peer table
def obs(peer: str, created: float, seq: int, received: datetime = NOW) -> PeerObservation:
    return PeerObservation(peer, 0.5, 0.8, seq, NOW + timedelta(seconds=created), received)


def test_peer_table_orders_by_created_at_then_seq():
    t = PeerTable(10, 30)
    assert t.update(obs("p", 10, 5))
    assert not t.update(obs("p", 5, 99))   # older created_at loses despite a higher seq
    assert t.update(obs("p", 20, 0))       # restarted peer: seq reset, newer created_at wins
    assert not t.update(obs("p", 20, 0))   # exact duplicate
    assert t.update(obs("p", 20, 1))       # seq breaks ties


def test_peer_table_recovers_from_backward_clock_jump():
    t = PeerTable(10, 30)
    t.update(obs("p", 1000, 1))
    assert not t.update(obs("p", 0, 1, received=NOW + timedelta(seconds=10)))
    assert t.update(obs("p", 0, 2, received=NOW + timedelta(seconds=31)))


def test_peer_table_snapshot_filters_stale_and_sorts():
    t = PeerTable(10, 30)
    t.update(obs("b", 0, 1))
    t.update(obs("a", 0, 1))
    t.update(obs("old", 0, 1, received=NOW - timedelta(seconds=60)))
    assert [o.peer_id for o in t.snapshot(NOW)] == ["a", "b"]


def test_peer_table_evicts_oldest_when_full():
    t = PeerTable(2, 30)
    t.update(obs("a", 0, 1, received=NOW))
    t.update(obs("b", 0, 1, received=NOW + timedelta(seconds=1)))
    t.update(obs("c", 0, 1, received=NOW + timedelta(seconds=2)))
    assert len(t) == 2 and [o.peer_id for o in t.snapshot(NOW + timedelta(seconds=2))] == ["b", "c"]


# ----------------------------------------------------------------- run loop
def test_run_processes_source_and_exits_cleanly(make_service, bridge):
    service = make_service()
    stop = threading.Event()
    code = service.run(iter([metrics_json(0.1, 1), None, b"\n", metrics_json(0.1, 2)]), stop)
    assert code == 0 and stop.is_set() and len(bridge.published) == 2


def test_run_fails_fast_when_bridge_is_down(ledger, clock):
    dead = FakeBridge().start()
    url = dead.url
    dead.stop()
    service = Microservice(ServiceConfig(ledger_path="x", startup_timeout_s=0.3),
                           bridge=BridgeClient(url, backoff_base_s=0.01, backoff_cap_s=0.02),
                           ledger=ledger, inference=FakeInference(), clock=clock)
    assert service.run(iter([]), threading.Event()) == 2


def test_run_exits_3_when_identity_changes(make_service, bridge):
    service = make_service(health_interval_s=0.01)
    stop = threading.Event()

    def source():
        yield metrics_json(0.1, 1)
        bridge.peer_id = "12D3KooWChangedPeer0000000000000000000000000000"
        import time
        time.sleep(0.05)
        yield None
        yield metrics_json(0.1, 2)

    assert service.run(source(), stop) == 3