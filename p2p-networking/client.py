"""Python client for the p2p-networking HTTP bridge.

    client = BridgeClient("http://127.0.0.1:8080")
    client.on_message(lambda from_peer, data: print(from_peer, data))
    client.start()
    client.publish({"node": "n1", "congestion": 0.7})

Transport only: payloads are opaque bytes (valid JSON, 1..65536 bytes).
Schema/version/digest checks belong to the caller (serializer.parse_envelope).
Standard library only.
"""
import base64
import json
import logging
import threading
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Optional, Union

log = logging.getLogger("bridge_client")

MAX_PAYLOAD_BYTES = 65536

MessageCallback = Callable[[str, bytes], None]  # (from_peer_id, raw_json_bytes)
DroppedCallback = Callable[[int], None]         # (number_of_missed_messages)


class BridgeError(Exception):
    pass


class BridgeClient:
    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8080",
        poll_timeout_ms: int = 25000,
        max_batch: int = 100,
    ):
        self.base_url = base_url.rstrip("/")
        self.poll_timeout_ms = poll_timeout_ms
        self.max_batch = max_batch
        self._handlers: list[MessageCallback] = []
        self._on_dropped: Optional[DroppedCallback] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ---- public API ----------------------------------------------------

    def publish(self, msg: Union[dict, list, bytes, str]) -> None:
        """Broadcast msg to all peers. dict/list are JSON-encoded; bytes/str are sent as-is."""
        if isinstance(msg, (dict, list)):
            raw = json.dumps(msg, separators=(",", ":")).encode()
        elif isinstance(msg, str):
            raw = msg.encode()
        else:
            raw = bytes(msg)
        if not 1 <= len(raw) <= MAX_PAYLOAD_BYTES:
            raise ValueError(f"payload must be 1..{MAX_PAYLOAD_BYTES} bytes, got {len(raw)}")
        body = json.dumps({"data": base64.b64encode(raw).decode()}).encode()
        req = urllib.request.Request(
            f"{self.base_url}/publish", body, method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            urllib.request.urlopen(req, timeout=10).read()
        except urllib.error.HTTPError as e:
            raise BridgeError(f"publish failed ({e.code}): {e.read().decode(errors='replace')}") from e
        except (urllib.error.URLError, OSError) as e:
            raise BridgeError(f"bridge unreachable: {e}") from e

    def on_message(self, cb: MessageCallback) -> None:
        """Register a callback invoked as cb(from_peer, data_bytes) for each incoming message."""
        self._handlers.append(cb)

    def on_dropped(self, cb: DroppedCallback) -> None:
        """Optional: called with the number of messages lost when the client fell behind the ring."""
        self._on_dropped = cb

    def health(self) -> dict:
        return self._get_json("/health", timeout=5)

    def peers(self) -> dict:
        return self._get_json("/peers", timeout=5)

    def start(self) -> None:
        if self._thread and self._thread.is_alive() and not self._stop.is_set():
            return
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._poll_loop, args=(self._stop,), name="bridge-poll", daemon=True
        )
        self._thread.start()
        
    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    # ---- internals -----------------------------------------------------

    def _get_json(self, path: str, timeout: float) -> dict:
        try:
            with urllib.request.urlopen(f"{self.base_url}{path}", timeout=timeout) as r:
                return json.load(r)
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise BridgeError(f"GET {path} failed: {e}") from e

    def _poll_loop(self, stop: threading.Event) -> None:
        cursor = 1
        boot_id: Optional[str] = None
        backoff = 0.5
        while not stop.is_set():
            qs = urllib.parse.urlencode(
                {"cursor": cursor, "max": self.max_batch, "timeout_ms": self.poll_timeout_ms}
            )
            try:
                resp = self._get_json(f"/subscribe?{qs}", timeout=self.poll_timeout_ms / 1000 + 5)
            except Exception as e:  # bridge down/restarting, socket timeout, bad JSON
                log.warning("subscribe failed (retry in %.1fs): %s", backoff, e)
                if stop.wait(backoff):
                    return
                backoff = min(backoff * 2, 10)
                continue
            backoff = 0.5

            # Go node restarted: its cursors restarted at 1, so our old cursor is meaningless
            # (it would wait forever). Reset and re-poll from the start of the new ring.
            if boot_id is not None and resp["boot_id"] != boot_id:
                log.warning("bridge restarted (boot_id %s -> %s); resetting cursor", boot_id, resp["boot_id"])
                boot_id = resp["boot_id"]
                cursor = 1
                continue
            boot_id = resp["boot_id"]

            if resp["dropped"] > 0:
                log.warning("fell behind the ring: %d messages lost", resp["dropped"])
                if self._on_dropped:
                    self._safe(self._on_dropped, resp["dropped"])

            for m in resp["messages"]:
                try:
                    data = base64.b64decode(m["data"])
                except Exception:
                    log.warning("skipping message with bad base64 (cursor %s)", m.get("cursor"))
                    continue
                for cb in list(self._handlers):
                    self._safe(cb, m["from"], data)

            cursor = resp["next_cursor"]

    @staticmethod
    def _safe(cb, *args) -> None:
        try:
            cb(*args)
        except Exception:
            log.exception("callback raised")


if __name__ == "__main__":
    # Manual check against the docker cluster:
    #   python client.py        -> listens on node2 (8081), publishes via node1 (8080)
    import time

    logging.basicConfig(level=logging.INFO)
    rx = BridgeClient("http://127.0.0.1:8081")
    rx.on_message(lambda p, d: print("recv from", p[:12], d))
    rx.start()
    tx = BridgeClient("http://127.0.0.1:8080")
    time.sleep(1)
    tx.publish({"hello": "world"})
    time.sleep(2)
    rx.stop()