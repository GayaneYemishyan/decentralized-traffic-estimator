"""Unit tests for src/bridge_client.py, run against tests/fake_bridge.py."""
from __future__ import annotations

import json
import logging
import random
import socket
import threading
import time

import pytest
from fake_bridge import FakeBridge

from src import serializer as s
from src.bridge_client import (
    MAX_MESSAGE_BYTES,
    BridgeClient,
    BridgeProtocolError,
    BridgeRejectedError,
    BridgeUnavailableError,
    CircuitBreaker,
    CircuitOpenError,
)


class FakeClock:
    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


@pytest.fixture
def bridge():
    with FakeBridge() as b:
        yield b


@pytest.fixture
def sleeps() -> list[float]:
    return []


@pytest.fixture
def client(bridge, sleeps):
    """Fast client: sleeps are recorded instead of slept."""
    return BridgeClient(bridge.url, request_timeout_s=1.0, poll_grace_s=1.0, publish_attempts=3,
                        sleep=sleeps.append, rng=random.Random(0))


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


# ================================================================= circuit breaker
def test_breaker_opens_after_threshold():
    clock = FakeClock()
    cb = CircuitBreaker(failure_threshold=3, reset_timeout_s=10, clock=clock)
    for _ in range(2):
        assert cb.allow()
        cb.record_failure()
    assert cb.state == "closed"
    assert cb.allow()
    cb.record_failure()
    assert cb.state == "open" and not cb.allow()


def test_success_resets_failure_count():
    cb = CircuitBreaker(failure_threshold=2, clock=FakeClock())
    cb.record_failure()
    cb.record_success()
    cb.record_failure()
    assert cb.state == "closed"


def test_breaker_half_open_single_probe_then_close():
    clock = FakeClock()
    cb = CircuitBreaker(failure_threshold=1, reset_timeout_s=10, clock=clock)
    cb.record_failure()
    clock.advance(9.9)
    assert not cb.allow()
    clock.advance(0.2)
    assert cb.state == "half_open"
    assert cb.allow()           # the probe
    assert not cb.allow()       # nobody else while the probe is in flight
    cb.record_success()
    assert cb.state == "closed" and cb.allow()


def test_breaker_failed_probe_reopens():
    clock = FakeClock()
    cb = CircuitBreaker(failure_threshold=1, reset_timeout_s=10, clock=clock)
    cb.record_failure()
    clock.advance(11)
    assert cb.allow()
    cb.record_failure()
    assert cb.state == "open"
    clock.advance(9)
    assert not cb.allow()
    clock.advance(2)
    assert cb.allow()


def test_breaker_rejects_bad_threshold():
    with pytest.raises(ValueError):
        CircuitBreaker(failure_threshold=0)


# ================================================================= construction
def test_base_url_forms(monkeypatch):
    assert BridgeClient("127.0.0.1:9").base_url == "http://127.0.0.1:9"
    assert BridgeClient("http://host:1/").base_url == "http://host:1"
    monkeypatch.setenv("BRIDGE_ADDR", "10.0.0.5:7000")
    assert BridgeClient().base_url == "http://10.0.0.5:7000"
    monkeypatch.delenv("BRIDGE_ADDR")
    assert BridgeClient().base_url == "http://127.0.0.1:8080"


# ================================================================= publish
def test_publish_delivers_exact_bytes(client, bridge):
    data = b'{"k":"\xc3\xa9"}'
    client.publish(data)
    assert bridge.published == [data]


def test_publish_empty_rejected_locally(client, bridge):
    with pytest.raises(BridgeRejectedError) as exc:
        client.publish(b"")
    assert exc.value.status == 400 and bridge.request_count() == 0


def test_publish_oversize_rejected_locally(client, bridge):
    with pytest.raises(BridgeRejectedError) as exc:
        client.publish(b"x" * (MAX_MESSAGE_BYTES + 1))
    assert exc.value.status == 413 and bridge.request_count() == 0
    boundary = b'"' + b"a" * (MAX_MESSAGE_BYTES - 2) + b'"'  # valid JSON, exactly the limit
    assert len(boundary) == MAX_MESSAGE_BYTES
    client.publish(boundary)  # the boundary is accepted
    assert bridge.published == [boundary]


def test_publish_non_json_rejected_by_bridge_and_not_retried(client, bridge):
    """The Go bridge accepts only well-formed JSON (validPayload) and answers 400."""
    with pytest.raises(BridgeRejectedError) as exc:
        client.publish(b"definitely not json")
    assert exc.value.status == 400 and "JSON" in exc.value.detail
    assert bridge.request_count("/publish") == 1 and bridge.published == []


def test_publish_requires_bytes(client):
    with pytest.raises(TypeError):
        client.publish("text")  # type: ignore[arg-type]


def test_publish_retries_transient_5xx(client, bridge, sleeps):
    bridge.fail_next(2, 503)
    client.publish(b"{}")
    assert bridge.request_count("/publish") == 3 and len(bridge.published) == 1
    assert len(sleeps) == 2 and sleeps[1] > 0


def test_publish_backoff_grows_and_is_capped(bridge, sleeps):
    c = BridgeClient(bridge.url, publish_attempts=6, backoff_base_s=0.1, backoff_cap_s=0.5,
                     sleep=sleeps.append, rng=random.Random(1),
                     breaker=CircuitBreaker(failure_threshold=100))
    bridge.fail_next(5, 503)
    c.publish(b"{}")
    ceilings = [min(0.5, 0.1 * 2 ** i) for i in range(5)]
    for delay, ceiling in zip(sleeps, ceilings):
        assert ceiling / 2 <= delay <= ceiling


def test_publish_retries_dropped_connection(client, bridge):
    bridge.drop_next(1)
    client.publish(b"{}")
    assert bridge.request_count("/publish") == 2


def test_publish_gives_up_after_attempts(client, bridge, sleeps):
    bridge.fail_next(10, 500)
    with pytest.raises(BridgeUnavailableError, match="HTTP 500"):
        client.publish(b"{}")
    assert bridge.request_count("/publish") == 3 and len(sleeps) == 2


def test_publish_4xx_not_retried(client, bridge):
    bridge.fail_next(1, 400)
    with pytest.raises(BridgeRejectedError) as exc:
        client.publish(b"{}")
    assert exc.value.status == 400 and bridge.request_count("/publish") == 1


def test_4xx_does_not_count_against_breaker(bridge):
    cb = CircuitBreaker(failure_threshold=2)
    c = BridgeClient(bridge.url, breaker=cb, sleep=lambda s: None)
    for _ in range(5):
        bridge.fail_next(1, 400)
        with pytest.raises(BridgeRejectedError):
            c.publish(b"{}")
    assert cb.state == "closed"


def test_publish_connection_refused(sleeps):
    c = BridgeClient(f"127.0.0.1:{free_port()}", sleep=sleeps.append)
    with pytest.raises(BridgeUnavailableError):
        c.publish(b"{}")


def test_publish_timeout(bridge, sleeps):
    c = BridgeClient(bridge.url, request_timeout_s=0.1, publish_attempts=1, sleep=sleeps.append)
    bridge.delay_next(0.6)
    t0 = time.monotonic()
    with pytest.raises(BridgeUnavailableError):
        c.publish(b"{}")
    assert time.monotonic() - t0 < 0.5


def test_publish_unexpected_reply_is_protocol_error(client, bridge):
    bridge.garbage_next(1)
    with pytest.raises(BridgeProtocolError):
        client.publish(b"{}")
    assert bridge.request_count("/publish") == 1  # protocol errors are not retried


@pytest.mark.parametrize("reply", [{"ok": False}, {"status": "queued"}, {}, {"ok": "true"}])
def test_publish_requires_ok_true(client, bridge, reply):
    bridge.reply_next(reply)
    with pytest.raises(BridgeProtocolError, match="unexpected response"):
        client.publish(b"{}")


def test_publish_ignores_proxy_env(bridge, monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
    BridgeClient(bridge.url).publish(b"{}")
    assert len(bridge.published) == 1


# ================================================================= breaker + client
def test_open_circuit_fails_fast_then_recovers(bridge, sleeps):
    clock = FakeClock()
    cb = CircuitBreaker(failure_threshold=2, reset_timeout_s=10, clock=clock)
    c = BridgeClient(bridge.url, publish_attempts=1, breaker=cb, sleep=sleeps.append)
    bridge.fail_next(2, 503)
    for _ in range(2):
        with pytest.raises(BridgeUnavailableError):
            c.publish(b"{}")
    before = bridge.request_count()
    with pytest.raises(CircuitOpenError):
        c.publish(b"{}")
    assert bridge.request_count() == before  # no network call while open
    clock.advance(11)
    c.publish(b"{}")  # probe succeeds
    assert cb.state == "closed" and len(bridge.published) == 1


def test_circuit_open_is_not_retried(bridge, sleeps):
    cb = CircuitBreaker(failure_threshold=1, reset_timeout_s=60, clock=FakeClock())
    c = BridgeClient(bridge.url, publish_attempts=5, breaker=cb, sleep=sleeps.append)
    bridge.fail_next(1, 500)
    with pytest.raises(BridgeUnavailableError):
        c.publish(b"{}")  # opens the breaker, attempt 2 hits CircuitOpenError
    assert bridge.request_count("/publish") == 1


# ================================================================= poll
def test_poll_returns_messages_and_cursor(client, bridge):
    bridge.inject(b"one")
    bridge.inject(b"two")
    r = client.poll(0, timeout_ms=200)
    assert [m.data for m in r.messages] == [b"one", b"two"]
    assert [m.cursor for m in r.messages] == [1, 2]
    assert r.next_cursor == 3 and r.dropped == 0 and not r.restarted  # "next cursor to read"
    assert client.poll(r.next_cursor, timeout_ms=50).messages == []
    bridge.inject(b"three")
    assert [m.data for m in client.poll(r.next_cursor, timeout_ms=100).messages] == [b"three"]  # no repeats


def test_poll_message_fields_follow_the_bridge_wire_format(client, bridge):
    bridge.inject(b"payload", from_peer="12D3KooWAuthor")
    msg = client.poll(0, timeout_ms=100).messages[0]
    assert (msg.from_peer, msg.data, msg.cursor) == ("12D3KooWAuthor", b"payload", 1)
    assert not hasattr(msg, "received_at")


def test_poll_empty_times_out_quickly(client):
    t0 = time.monotonic()
    r = client.poll(0, timeout_ms=150)
    assert r.messages == [] and r.next_cursor == 1  # the bridge clamps cursor 0 to 1
    assert 0.1 < time.monotonic() - t0 < 1.0


def test_poll_wakes_on_new_message(client, bridge):
    threading.Timer(0.1, bridge.inject, args=(b"late",)).start()
    t0 = time.monotonic()
    r = client.poll(0, timeout_ms=5000)
    assert [m.data for m in r.messages] == [b"late"]
    assert time.monotonic() - t0 < 2.0


def test_poll_max_messages(client, bridge):
    for i in range(5):
        bridge.inject(bytes([65 + i]))
    r = client.poll(0, timeout_ms=100, max_messages=2)
    assert len(r.messages) == 2 and r.next_cursor == 3


def test_poll_reports_dropped(bridge, sleeps):
    small = FakeBridge(ring_size=3).start()
    try:
        c = BridgeClient(small.url, sleep=sleeps.append)
        for i in range(5):
            small.inject(bytes([65 + i]))
        r = c.poll(0, timeout_ms=100)
        assert r.dropped == 2 and [m.cursor for m in r.messages] == [3, 4, 5]
    finally:
        small.stop()


def test_poll_detects_bridge_restart(client, bridge):
    bridge.inject(b"a")
    bridge.inject(b"b")
    bridge.inject(b"c")
    r = client.poll(0, timeout_ms=100)
    assert r.next_cursor == 4
    bridge.restart()                  # cursors go back to 1
    bridge.inject(b"new1")
    bridge.inject(b"new2")
    r = client.poll(4, timeout_ms=100)  # the stale cursor would hide both new messages
    assert r.restarted and [m.data for m in r.messages] == [b"new1", b"new2"]
    assert r.next_cursor == 3


def test_poll_skips_malformed_entries(client, bridge):
    bridge.inject(b"good")
    bridge.inject_raw({"from": "p", "data": "***not base64***"})
    bridge.inject_raw({"from": "p"})  # missing data
    bridge.inject_raw({"from_peer": "p", "data": "e30="})  # old field name: not the wire format
    r = client.poll(0, timeout_ms=100)
    assert [m.data for m in r.messages] == [b"good"] and r.malformed == 3
    assert r.next_cursor == 5  # the cursor still advances past the bad entries


def test_poll_garbage_reply(client, bridge):
    bridge.garbage_next(1)
    with pytest.raises(BridgeProtocolError):
        client.poll(0, timeout_ms=50)


@pytest.mark.parametrize("kwargs", [{"cursor": -1}, {"timeout_ms": 30001}, {"max_messages": 0}, {"max_messages": 501}])
def test_poll_argument_validation(client, kwargs):
    with pytest.raises(ValueError):
        client.poll(**kwargs)


# ================================================================= health / readiness
def test_health_and_peers(client, bridge):
    h = client.health()
    assert h["peer_id"] == bridge.peer_id and h["boot_id"] == bridge.boot_id
    assert h["topic_peers"] == 1 and h["peer_count"] == 1 and h["ready"] is True
    assert client.peers() == bridge.peers


@pytest.mark.parametrize("reply", [{"ready": True}, {"peer_id": "p"}, {"peer_id": 5, "boot_id": "b"}])
def test_health_requires_peer_id_and_boot_id(client, bridge, reply):
    bridge.reply_next(reply)
    with pytest.raises(BridgeProtocolError, match="peer_id/boot_id"):
        client.health()


def test_health_garbage(client, bridge):
    bridge.garbage_next(1)
    with pytest.raises(BridgeProtocolError):
        client.health()


def test_lone_node_is_up_even_though_the_bridge_says_not_ready(bridge):
    """The Go bridge reports ready=false until a topic peer appears. An offline-first node must still start."""
    bridge.topic_peers.clear()
    c = BridgeClient(bridge.url, backoff_base_s=0.01, backoff_cap_s=0.02)
    assert c.health()["ready"] is False
    t0 = time.monotonic()
    assert c.wait_ready(timeout_s=3)
    assert time.monotonic() - t0 < 1.0


def test_wait_ready_waits_for_the_bridge_to_start_answering(bridge):
    c = BridgeClient(bridge.url, backoff_base_s=0.01, backoff_cap_s=0.02,
                     breaker=CircuitBreaker(failure_threshold=50))
    bridge.drop_next(2)
    bridge.fail_next(2, 503)
    bridge.garbage_next(1)
    assert c.wait_ready(timeout_s=3)


def test_wait_ready_require_peers_waits_for_topic_peers(bridge):
    bridge.topic_peers.clear()
    c = BridgeClient(bridge.url, backoff_base_s=0.01, backoff_cap_s=0.02)
    threading.Timer(0.15, bridge.topic_peers.append, args=("12D3KooWNewPeer",)).start()
    assert c.wait_ready(timeout_s=3, require_peers=True)


def test_wait_ready_require_peers_times_out_alone(bridge):
    bridge.topic_peers.clear()
    c = BridgeClient(bridge.url, backoff_base_s=0.01, backoff_cap_s=0.02)
    assert c.wait_ready(timeout_s=0.2, require_peers=True) is False


def test_wait_ready_stops_on_event(bridge):
    c = BridgeClient(bridge.url, backoff_base_s=0.01, backoff_cap_s=0.02)
    bridge.topic_peers.clear()
    stop = threading.Event()
    threading.Timer(0.1, stop.set).start()
    t0 = time.monotonic()
    assert c.wait_ready(timeout_s=10, stop=stop, require_peers=True) is False
    assert time.monotonic() - t0 < 2.0


def test_wait_ready_survives_unreachable_bridge():
    c = BridgeClient(f"127.0.0.1:{free_port()}", backoff_base_s=0.01, backoff_cap_s=0.02)
    assert c.wait_ready(timeout_s=0.2) is False


# ================================================================= listen loop
class Collector:
    def __init__(self) -> None:
        self.items: list[bytes] = []
        self._cv = threading.Condition()

    def __call__(self, msg) -> None:
        with self._cv:
            self.items.append(msg.data)
            self._cv.notify_all()

    def wait_for(self, n: int, timeout: float = 5.0) -> bool:
        with self._cv:
            return self._cv.wait_for(lambda: len(self.items) >= n, timeout)


@pytest.fixture
def running_listener(bridge):
    """Start listen() in a thread; yields (collector, stop_event, thread)."""
    c = BridgeClient(bridge.url, backoff_base_s=0.01, backoff_cap_s=0.05, poll_grace_s=1.0,
                     breaker=CircuitBreaker(failure_threshold=50))  # breaker has its own tests
    stop = threading.Event()
    col = Collector()
    t = threading.Thread(target=c.listen, args=(col, stop), kwargs={"timeout_ms": 100}, daemon=True)
    t.start()
    yield col, stop, t
    stop.set()
    t.join(timeout=3)


def test_listen_delivers_in_order(running_listener, bridge):
    col, _, _ = running_listener
    for d in (b"1", b"2", b"3"):
        bridge.inject(d)
    assert col.wait_for(3) and col.items == [b"1", b"2", b"3"]


def test_listen_stops_promptly(running_listener):
    _, stop, t = running_listener
    time.sleep(0.05)
    stop.set()
    t.join(timeout=2)
    assert not t.is_alive()


def test_listen_survives_outage_and_never_raises(running_listener, bridge):
    col, _, t = running_listener
    bridge.fail_next(4, 503)
    bridge.drop_next(2)
    bridge.garbage_next(1)
    bridge.inject(b"after-outage")
    assert col.wait_for(1) and t.is_alive()


def test_listen_survives_handler_exceptions(bridge, caplog):
    seen: list[bytes] = []

    def handler(msg) -> None:
        seen.append(msg.data)
        if msg.data == b"boom":
            raise RuntimeError("handler bug")

    c = BridgeClient(bridge.url, backoff_base_s=0.01, backoff_cap_s=0.05)
    stop = threading.Event()
    t = threading.Thread(target=c.listen, args=(handler, stop), kwargs={"timeout_ms": 100}, daemon=True)
    with caplog.at_level(logging.ERROR):
        t.start()
        for d in (b"boom", b"ok"):
            bridge.inject(d)
        deadline = time.monotonic() + 3
        while len(seen) < 2 and time.monotonic() < deadline:
            time.sleep(0.02)
        stop.set()
        t.join(timeout=3)
    assert seen == [b"boom", b"ok"] and "handler failed" in caplog.text


def test_listen_recovers_after_bridge_restart(running_listener, bridge):
    col, _, _ = running_listener
    bridge.inject(b"before")
    assert col.wait_for(1)
    bridge.restart()
    bridge.inject(b"after-restart")
    assert col.wait_for(2) and col.items == [b"before", b"after-restart"]


def test_listen_with_circuit_open_does_not_spin(bridge):
    cb = CircuitBreaker(failure_threshold=1, reset_timeout_s=60)
    c = BridgeClient(bridge.url, breaker=cb, backoff_base_s=0.05, backoff_cap_s=0.05)
    bridge.fail_next(1, 500)
    stop = threading.Event()
    t = threading.Thread(target=c.listen, args=(lambda m: None, stop), kwargs={"timeout_ms": 50}, daemon=True)
    t.start()
    time.sleep(0.5)
    stop.set()
    t.join(timeout=2)
    assert bridge.request_count("/subscribe") == 1  # open circuit: no further network calls
    assert cb.state == "open"


# ================================================================= end to end with the serializer
STATE = json.loads((s.contracts_dir() / "examples" / "state.example.json").read_text())


def test_envelope_survives_publish(client, bridge):
    raw = s.encode_envelope(s.build_envelope(STATE, "node-a"))
    client.publish(raw)
    assert bridge.published == [raw]
    assert s.parse_envelope(bridge.published[0]).payload == STATE


def test_envelope_received_via_poll(client, bridge):
    raw = s.encode_envelope(s.build_envelope({**STATE, "node_id": "node-b"}, "node-b"))
    bridge.inject(raw)
    msg = client.poll(0, timeout_ms=100).messages[0]
    assert s.parse_envelope(msg.data).origin_node_id == "node-b"
