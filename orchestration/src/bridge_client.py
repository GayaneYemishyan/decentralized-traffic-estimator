"""Python client for the Go libp2p bridge (see contracts/bridge_api.md).

Goals: a bridge that is slow, restarting, or gone must never crash or block the
orchestration loop. Hence:
- every call has a timeout;
- publish retries with exponential backoff + jitter;
- a circuit breaker makes calls fail fast while the bridge is down;
- :meth:`BridgeClient.listen` never raises: it backs off and keeps going;
- a changed ``boot_id`` (bridge restarted, cursors reset) is detected and the
    client restarts from cursor 0 instead of silently losing messages.

Stdlib only (urllib), so the edge container stays small. The client is
transport-level: it moves bytes and knows nothing about envelopes.
"""
from __future__ import annotations

import base64
import binascii
import http.client
import json
import logging
import os
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

log = logging.getLogger(__name__)

MAX_MESSAGE_BYTES = 65536
DEFAULT_ADDR = "127.0.0.1:8080"


# --------------------------------------------------------------------------- errors
class BridgeError(Exception):
    """Base class for bridge failures."""


class BridgeUnavailableError(BridgeError):
    """Network failure, timeout or 5xx/429. Retryable."""


class CircuitOpenError(BridgeUnavailableError):
    """Call refused locally because the circuit breaker is open."""


class BridgeRejectedError(BridgeError):
    """The bridge refused the request (4xx). Retrying will not help."""

    def __init__(self, status: int, detail: str) -> None:
        self.status = status
        self.detail = detail
        super().__init__(f"bridge rejected request: HTTP {status}: {detail}")


class BridgeProtocolError(BridgeError):
    """The bridge answered, but not according to bridge_api.md."""


# --------------------------------------------------------------------------- types
@dataclass(frozen=True)
class BridgeMessage:
    """One message received from a peer."""

    cursor: int
    from_peer: str  # libp2p peer ID of the message author (JSON key "from" on the wire)
    data: bytes


@dataclass(frozen=True)
class PollResult:
    """Result of one long-poll."""

    messages: list[BridgeMessage]
    next_cursor: int
    dropped: int = 0  # messages that fell out of the bridge ring buffer unread
    malformed: int = 0  # entries skipped because they were not valid per the spec
    restarted: bool = False  # bridge restarted; this result was re-read from cursor 0


# --------------------------------------------------------------------------- breaker
class CircuitBreaker:
    """Classic closed / open / half-open breaker. Thread-safe.

    After ``failure_threshold`` consecutive failures it opens; after
    ``reset_timeout_s`` exactly one probe call is allowed (half-open). A probe
    success closes it, a probe failure re-opens it.
    """

    def __init__(
        self,
        failure_threshold: int = 5,
        reset_timeout_s: float = 10.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be >= 1")
        self._threshold = failure_threshold
        self._reset = reset_timeout_s
        self._clock = clock
        self._lock = threading.Lock()
        self._failures = 0
        self._opened_at: float | None = None
        self._probing = False

    @property
    def state(self) -> str:
        with self._lock:
            if self._opened_at is None:
                return "closed"
            if self._probing or self._clock() - self._opened_at >= self._reset:
                return "half_open"
            return "open"

    def allow(self) -> bool:
        """True if a call may proceed. Every allowed call MUST report its outcome."""
        with self._lock:
            if self._opened_at is None:
                return True
            if self._clock() - self._opened_at >= self._reset and not self._probing:
                self._probing = True
                return True
            return False

    def record_success(self) -> None:
        with self._lock:
            self._failures, self._opened_at, self._probing = 0, None, False

    def record_failure(self) -> None:
        with self._lock:
            if self._probing:  # failed probe: back to open
                self._opened_at, self._probing = self._clock(), False
            elif self._opened_at is None:
                self._failures += 1
                if self._failures >= self._threshold:
                    self._opened_at = self._clock()
                    log.warning("bridge circuit opened after %d failures", self._failures)


# --------------------------------------------------------------------------- client
def _normalize_base(addr: str) -> str:
    return (addr if "://" in addr else f"http://{addr}").rstrip("/")


class BridgeClient:
    """Resilient client for the local Go bridge."""

    def __init__(
        self,
        base_url: str | None = None,
        *,
        request_timeout_s: float = 1.0,
        poll_grace_s: float = 2.0,
        publish_attempts: int = 3,
        backoff_base_s: float = 0.2,
        backoff_cap_s: float = 5.0,
        breaker: CircuitBreaker | None = None,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
    ) -> None:
        self._base = _normalize_base(base_url or os.environ.get("BRIDGE_ADDR", DEFAULT_ADDR))
        self._timeout = request_timeout_s
        self._poll_grace = poll_grace_s
        self._publish_attempts = max(1, publish_attempts)
        self._backoff_base = backoff_base_s
        self._backoff_cap = backoff_cap_s
        self._breaker = breaker or CircuitBreaker()
        self._sleep = sleep
        self._rng = rng or random.Random()
        # Loopback only: ignore HTTP(S)_PROXY environment variables.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self._boot_lock = threading.Lock()
        self._boot_id: str | None = None  # tracked by poll() only (see poll)

    @property
    def base_url(self) -> str:
        return self._base

    @property
    def breaker(self) -> CircuitBreaker:
        return self._breaker

    # ---- low level
    def _backoff(self, attempt: int) -> float:
        """Exponential backoff with 'equal jitter': half fixed, half random."""
        ceiling = min(self._backoff_cap, self._backoff_base * (2 ** min(attempt, 16)))
        return ceiling / 2 + self._rng.uniform(0, ceiling / 2)

    def _request(self, method: str, path: str, *, body: Any = None, timeout: float) -> dict[str, Any]:
        if not self._breaker.allow():
            raise CircuitOpenError(f"circuit open: not calling {method} {path}")
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json"} if data is not None else {}
        req = urllib.request.Request(self._base + path, data=data, method=method, headers=headers)
        try:
            with self._opener.open(req, timeout=timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            detail = _error_detail(exc)
            if exc.code >= 500 or exc.code == 429:
                self._breaker.record_failure()
                raise BridgeUnavailableError(f"{method} {path}: HTTP {exc.code}: {detail}") from exc
            self._breaker.record_success()  # the bridge is alive, it just refused us
            raise BridgeRejectedError(exc.code, detail) from exc
        except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
            self._breaker.record_failure()
            raise BridgeUnavailableError(f"{method} {path}: {exc}") from exc
        except BaseException:
            self._breaker.record_failure()
            raise
        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError("expected a JSON object")
        except ValueError as exc:
            self._breaker.record_failure()
            raise BridgeProtocolError(f"{method} {path}: invalid response: {exc}") from exc
        self._breaker.record_success()
        return payload

    # ---- publish
    def publish(self, data: bytes, *, attempts: int | None = None) -> None:
        """Broadcast opaque bytes to peers. Retries transient failures.

        A retry can duplicate a message whose first attempt succeeded but whose
        reply was lost; receivers de-duplicate on ``message_id``.
        """
        if not isinstance(data, (bytes, bytearray)):
            raise TypeError("data must be bytes")
        if not data:
            raise BridgeRejectedError(400, "empty message")
        if len(data) > MAX_MESSAGE_BYTES:
            raise BridgeRejectedError(413, f"message is {len(data)} bytes, limit {MAX_MESSAGE_BYTES}")
        body = {"data": base64.b64encode(bytes(data)).decode("ascii")}
        total = max(1, attempts if attempts is not None else self._publish_attempts)
        last: BridgeUnavailableError | None = None
        for attempt in range(total):
            try:
                resp = self._request("POST", "/publish", body=body, timeout=self._timeout)
            except CircuitOpenError:
                raise  # fail fast, do not hammer a dead bridge
            except BridgeUnavailableError as exc:
                last = exc
                if attempt + 1 < total:
                    delay = self._backoff(attempt)
                    log.warning("publish attempt %d/%d failed (%s); retrying in %.2fs",
                                attempt + 1, total, exc, delay)
                    self._sleep(delay)
                continue
            if resp.get("ok") is not True:
                raise BridgeProtocolError(f"POST /publish: unexpected response {resp!r}")
            return
        assert last is not None
        raise last

    # ---- subscribe
    def _poll_once(self, cursor: int, timeout_ms: int, max_messages: int) -> tuple[PollResult, bool]:
        query = urllib.parse.urlencode({"cursor": cursor, "timeout_ms": timeout_ms, "max": max_messages})
        resp = self._request("GET", f"/subscribe?{query}", timeout=timeout_ms / 1000 + self._poll_grace)
        try:
            boot_id = resp["boot_id"]
            next_cursor = resp["next_cursor"]
            dropped = resp.get("dropped", 0)
            entries = resp["messages"]
            if (not isinstance(boot_id, str) or not isinstance(entries, list)
                    or not isinstance(next_cursor, int) or isinstance(next_cursor, bool)
                    or not isinstance(dropped, int) or isinstance(dropped, bool)):
                raise TypeError("wrong field types")
        except (KeyError, TypeError) as exc:
            raise BridgeProtocolError(f"GET /subscribe: malformed response: {exc!r}") from exc

        messages: list[BridgeMessage] = []
        malformed = 0
        for entry in entries:
            try:
                messages.append(BridgeMessage(
                    cursor=_as_int(entry["cursor"]),
                    from_peer=_as_str(entry["from"]),
                    data=base64.b64decode(entry["data"], validate=True),
                ))
            except (KeyError, TypeError, ValueError, binascii.Error):
                malformed += 1
        if malformed:
            log.warning("skipped %d malformed message(s) from bridge", malformed)

        with self._boot_lock:
            changed = self._boot_id is not None and self._boot_id != boot_id
            self._boot_id = boot_id
        return PollResult(messages, next_cursor, dropped, malformed), changed

    def poll(self, cursor: int = 0, *, timeout_ms: int = 5000, max_messages: int = 100) -> PollResult:
        """One long-poll. Pass 0 first, then always pass back the previous ``next_cursor``.

        Treat cursors as opaque: the bridge defines them as "the next cursor to read".

        If the bridge restarted since the previous poll (``boot_id`` changed) its
        cursors were reset, so the poll is repeated from cursor 0 and the result is
        flagged ``restarted``. (Only poll tracks ``boot_id``; ``health`` does not,
        or the change would be hidden from here.) Raises :class:`BridgeError` on
        failure; use :meth:`listen` for a loop that never raises.
        """
        if cursor < 0 or not 0 <= timeout_ms <= 30000 or not 1 <= max_messages <= 500:
            raise ValueError("cursor >= 0, 0 <= timeout_ms <= 30000, 1 <= max_messages <= 500 required")
        result, changed = self._poll_once(cursor, timeout_ms, max_messages)
        if changed:
            log.warning("bridge restarted (boot_id changed); resetting cursor to 0")
            if cursor != 0:
                result, _ = self._poll_once(0, timeout_ms, max_messages)
            result = PollResult(result.messages, result.next_cursor, result.dropped,
                                result.malformed, restarted=True)
        elif result.next_cursor < cursor:
            raise BridgeProtocolError(f"cursor went backwards ({cursor} -> {result.next_cursor}) without a new boot_id")
        return result

    def listen(
        self,
        handler: Callable[[BridgeMessage], None],
        stop: threading.Event,
        *,
        cursor: int = 0,
        timeout_ms: int = 5000,
        max_messages: int = 100,
    ) -> None:
        """Deliver incoming messages to ``handler`` until ``stop`` is set. Never raises.

        Bridge errors trigger backoff and retry; an exception in ``handler`` is
        logged and does not stop the loop (one bad message must not kill the node).
        """
        failures = 0
        while not stop.is_set():
            try:
                result = self.poll(cursor, timeout_ms=timeout_ms, max_messages=max_messages)
            except BridgeError as exc:
                failures += 1
                delay = self._backoff(failures - 1)
                # An open circuit was already reported when it opened; keep the log quiet.
                level = logging.DEBUG if isinstance(exc, CircuitOpenError) else logging.WARNING
                log.log(level, "bridge poll failed (%s); retrying in %.2fs", exc, delay)
                stop.wait(delay)
                continue
            except Exception:  # defensive: the loop must survive anything
                failures += 1
                log.exception("unexpected error while polling bridge")
                stop.wait(self._backoff(failures - 1))
                continue
            failures = 0
            if result.dropped:
                log.warning("bridge dropped %d message(s) before they were read", result.dropped)
            cursor = result.next_cursor
            for msg in result.messages:
                try:
                    handler(msg)
                except Exception:
                    log.exception("handler failed for bridge message cursor=%d", msg.cursor)

    # ---- status
    def health(self) -> dict[str, Any]:
        """GET /health. Keys used: peer_id, boot_id, ready, topic_peers (int).

        ``ready`` means "the bridge is up"; connectivity is ``topic_peers``. A lone node
        has ``topic_peers == 0`` and must still work (offline-first).
        """
        resp = self._request("GET", "/health", timeout=self._timeout)
        if not isinstance(resp.get("peer_id"), str) or not isinstance(resp.get("boot_id"), str):
            raise BridgeProtocolError(f"GET /health: missing peer_id/boot_id: {resp!r}")
        return resp

    def peers(self) -> list[str]:
        """GET /peers: libp2p peer IDs currently connected."""
        resp = self._request("GET", "/peers", timeout=self._timeout)
        peers = resp.get("peers")
        if not isinstance(peers, list) or not all(isinstance(p, str) for p in peers):
            raise BridgeProtocolError(f"GET /peers: malformed response: {resp!r}")
        return peers

    def wait_ready(
        self,
        timeout_s: float = 30.0,
        stop: threading.Event | None = None,
        *,
        require_peers: bool = False,
    ) -> bool:
        """Block until the bridge answers /health (startup gating). False on timeout or stop.

        An offline-first node must work alone, so by default it does NOT wait for peers.
        With ``require_peers=True`` it also waits until at least one peer is subscribed
        to the topic (``topic_peers`` > 0), e.g. for integration tests.
        """
        stop = stop or threading.Event()
        deadline = time.monotonic() + timeout_s
        attempt = 0
        while not stop.is_set() and time.monotonic() < deadline:
            try:
                info = self.health()
                topic_peers = info.get("topic_peers", 0)
                if info.get("ready", True) is True and (
                    not require_peers or (isinstance(topic_peers, int) and topic_peers > 0)
                ):
                    return True
            except BridgeError as exc:
                log.info("waiting for bridge: %s", exc)
            stop.wait(min(self._backoff(attempt), max(0.0, deadline - time.monotonic())))
            attempt += 1
        return False


# --------------------------------------------------------------------------- helpers
def _as_str(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise TypeError("expected a non-empty string")
    return value


def _as_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("expected an integer")
    return value


def _error_detail(exc: urllib.error.HTTPError) -> str:
    try:
        text = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict) and "error" in parsed:
                return str(parsed["error"])
        except ValueError:
            pass
        return text[:200] or exc.reason
    except Exception:
        return str(exc.reason)