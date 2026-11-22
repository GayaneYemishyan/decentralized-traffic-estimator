"""In-process fake of the Go bridge, a faithful port of p2p-networking/bridge.go (PR #53).

Mirrors its observable behaviour so the Python client is tested against the real
wire format: JSON shapes ({"ok": true}, messages with cursor/from/data), cursors that mean
"next cursor to read" (starting at 1, clamped), the ring buffer with ``dropped``,
``ready`` meaning "at least one topic peer", ``validPayload`` (1..65536 bytes and valid
JSON, else 400) and ``boot_id``. It can also inject failures the real bridge will
produce: 5xx, dropped connections, slow replies, non-JSON replies, arbitrary JSON
replies and restarts (new ``boot_id``, cursors back to 1).

Reusable for integration tests (it lets the orchestration run with no Go binary).
"""
from __future__ import annotations

import base64
import binascii
import json
import socket
import threading
import time
import uuid
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

MAX_PAYLOAD = 65536
DEFAULT_POLL_MAX = 100
DEFAULT_POLL_WAIT = 25.0
MAX_POLL_WAIT = 30.0
MAX_PUBLISH_BODY = 96 << 10


def _valid_payload(data: bytes) -> str | None:
    """Port of validPayload() in transport.go. Returns an error message or None."""
    if len(data) < 1 or len(data) > MAX_PAYLOAD:
        return "payload size must be 1..65536 bytes"
    try:
        json.loads(data)
    except ValueError:
        return "payload is not valid JSON"
    return None


class FakeBridge:
    def __init__(self, *, ring_size: int = 1024,
                 peer_id: str = "12D3KooWFakeSelfPeer0000000000000000000000000000") -> None:
        self.ring_size = ring_size
        self.peer_id = peer_id
        self.peers: list[str] = ["12D3KooWFakeOtherPeer00000000000000000000000000"]
        self.topic_peers: list[str] = list(self.peers)  # ready == bool(topic_peers), as in handleHealth
        self.published: list[bytes] = []
        self.requests: list[tuple[str, str]] = []  # (method, path without query)
        self.boot_id = uuid.uuid4().hex[:16]
        self._ring: dict[int, dict] = {}
        self._last = 0
        self._cv = threading.Condition()
        self._faults: deque[tuple] = deque()
        self._closing = False
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:  # silence
                pass

            def _send(self, status: int, obj=None, raw: bytes | None = None) -> None:
                body = raw if raw is not None else json.dumps(obj).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _apply_fault(self) -> bool:
                """Returns True if the request was consumed by a fault."""
                with bridge._cv:
                    fault = bridge._faults.popleft() if bridge._faults else None
                if fault is None:
                    return False
                kind = fault[0]
                if kind == "status":
                    self._send(fault[1], {"error": "injected fault"})
                    return True
                if kind == "drop":
                    self.close_connection = True
                    try:
                        self.connection.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    return True
                if kind == "garbage":
                    self._send(200, raw=b"<html>not json</html>")
                    return True
                if kind == "json":
                    self._send(200, fault[1])
                    return True
                if kind == "delay":
                    time.sleep(fault[1])
                return False

            def _route(self, method: str) -> None:
                url = urlparse(self.path)
                with bridge._cv:
                    bridge.requests.append((method, url.path))
                if self._apply_fault():
                    return
                if url.path == "/publish":
                    return self._publish() if method == "POST" else self._send(405, {"error": "POST only"})
                if url.path == "/subscribe":
                    return self._subscribe(parse_qs(url.query))
                if method == "GET" and url.path == "/health":
                    return self._health()
                if method == "GET" and url.path == "/peers":
                    return self._send(200, {"peers": list(bridge.peers)})
                self._send(404, {"error": "not found"})

            def _health(self) -> None:
                self._send(200, {"ready": len(bridge.topic_peers) > 0, "peer_id": bridge.peer_id,
                                 "peer_count": len(bridge.peers), "topic_peers": len(bridge.topic_peers),
                                 "boot_id": bridge.boot_id})

            def _publish(self) -> None:
                length = int(self.headers.get("Content-Length", 0))
                if length > MAX_PUBLISH_BODY:
                    return self._send(400, {"error": "invalid request body"})
                try:
                    body = json.loads(self.rfile.read(length))
                    raw = base64.b64decode(body["data"], validate=True)
                except (ValueError, KeyError, TypeError, binascii.Error):
                    return self._send(400, {"error": "invalid request body"})
                if err := _valid_payload(raw):
                    return self._send(400, {"error": err})
                with bridge._cv:
                    bridge.published.append(raw)
                self._send(200, {"ok": True})

            def _subscribe(self, q: dict) -> None:
                def num(name: str, default: int) -> int:
                    try:
                        return int(q.get(name, [""])[0])
                    except ValueError:
                        return default
                cursor = max(num("cursor", 0), 1)
                max_n = DEFAULT_POLL_MAX
                if (v := num("max", 0)) > 0:
                    max_n = min(v, bridge.ring_size)
                wait = DEFAULT_POLL_WAIT
                if (v := num("timeout_ms", -1)) >= 0:
                    wait = min(v / 1000, MAX_POLL_WAIT)
                deadline = time.monotonic() + wait
                dropped = 0
                with bridge._cv:
                    while True:
                        oldest = 1 if bridge._last <= bridge.ring_size else bridge._last - bridge.ring_size + 1
                        if cursor < oldest:  # client fell behind the ring: report the gap explicitly
                            dropped += oldest - cursor
                            cursor = oldest
                        if cursor <= bridge._last:
                            stop = min(bridge._last, cursor + max_n - 1)
                            msgs = [bridge._ring[c] for c in range(cursor, stop + 1)]
                            payload = {"boot_id": bridge.boot_id, "messages": msgs,
                                       "next_cursor": cursor + len(msgs), "dropped": dropped}
                            break
                        remaining = deadline - time.monotonic()
                        if remaining <= 0 or bridge._closing:
                            payload = {"boot_id": bridge.boot_id, "messages": [],
                                       "next_cursor": cursor, "dropped": dropped}
                            break
                        bridge._cv.wait(remaining)
                self._send(200, payload)

            def do_GET(self) -> None:
                self._route("GET")

            def do_POST(self) -> None:
                self._route("POST")

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        kwargs={"poll_interval": 0.02}, daemon=True)

    # ---- lifecycle
    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def start(self) -> "FakeBridge":
        self._thread.start()
        return self

    def stop(self) -> None:
        with self._cv:
            self._closing = True
            self._cv.notify_all()
        self._server.shutdown()
        self._server.server_close()

    def __enter__(self) -> "FakeBridge":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    # ---- test controls
    @property
    def ready(self) -> bool:
        return len(self.topic_peers) > 0

    def inject(self, data: bytes, from_peer: str = "12D3KooWFakeOtherPeer00000000000000000000000000") -> int:
        """Pretend a peer sent ``data``. Returns the assigned cursor."""
        return self.inject_raw({"from": from_peer, "data": base64.b64encode(data).decode()})

    def inject_raw(self, entry: dict) -> int:
        """Append an arbitrary entry (to simulate malformed bridge output)."""
        with self._cv:
            self._last += 1
            self._ring[self._last] = {"cursor": self._last, **entry}
            self._ring.pop(self._last - self.ring_size, None)
            self._cv.notify_all()
            return self._last

    def restart(self) -> None:
        """Simulate a bridge restart: new boot_id, empty buffer, cursors back to 1."""
        with self._cv:
            self.boot_id = uuid.uuid4().hex[:16]
            self._ring.clear()
            self._last = 0
            self._cv.notify_all()

    def fail_next(self, n: int = 1, status: int = 503) -> None:
        with self._cv:
            self._faults.extend([("status", status)] * n)

    def drop_next(self, n: int = 1) -> None:
        with self._cv:
            self._faults.extend([("drop",)] * n)

    def garbage_next(self, n: int = 1) -> None:
        with self._cv:
            self._faults.extend([("garbage",)] * n)

    def reply_next(self, obj, n: int = 1) -> None:
        """Answer the next request(s) with HTTP 200 and this JSON."""
        with self._cv:
            self._faults.extend([("json", obj)] * n)

    def delay_next(self, seconds: float, n: int = 1) -> None:
        with self._cv:
            self._faults.extend([("delay", seconds)] * n)

    def request_count(self, path: str | None = None) -> int:
        with self._cv:
            return sum(1 for _, p in self.requests if path is None or p == path)
