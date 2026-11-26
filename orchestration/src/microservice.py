"""Microservice wrapper: the orchestration loop of one node (P4).

    metrics (NDJSON on stdin) -> validate -> stamp node_id -> inference (local state)
        -> ledger -> broadcast envelope (local scope only)
        -> regional estimate (local + fresh peers) -> ledger -> triggers -> actions -> ledger

    bridge /subscribe -> parse_envelope -> peer identity -> scope==local -> dedupe
        -> ledger -> peer table

Run:  python -m src.microservice --ledger /data/ledger.db < metrics.ndjson
Exit codes: 0 clean stop / EOF, 2 startup failure, 3 identity changed (restart me).
Logs go to stderr; stdout is not used.
"""
from __future__ import annotations

import argparse
import collections
import logging
import os
import queue
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, BinaryIO, Callable, Iterable, Iterator, Mapping, Sequence

from .bridge_client import BridgeClient, BridgeError, BridgeMessage, CircuitOpenError
from .inference_adapter import InferenceAdapter, InferenceUnavailableError, LilitInference, PeerObservation
from .ledger import DuplicateMessageError, Ledger, LedgerError
from .serializer import (
    ContractError,
    build_envelope,
    check_peer_identity,
    decode_json,
    encode_envelope,
    parse_envelope,
    validate_metrics,
    validate_state,
)
from .triggers import Action, ActionHandler, JsonlActionSink, TriggerConfig, TriggerEngine, dispatch, log_handler

log = logging.getLogger(__name__)


class ServiceError(Exception):
    """Startup or configuration failure."""


@dataclass(frozen=True)
class ServiceConfig:
    ledger_path: str
    node_id: str | None = None              # default: the bridge's libp2p peer ID
    enforce_peer_identity: bool = True      # origin_node_id == authoring peer
    startup_timeout_s: float = 30.0
    poll_timeout_ms: int = 5000
    peer_max_age_s: float = 30.0            # freshness, measured on OUR clock (received_at)
    max_created_age_s: float = 300.0        # replay guard on the peer's created_at (sanity bound)
    max_future_skew_s: float = 30.0
    max_peers: int = 1024
    seen_cache_size: int = 4096
    health_interval_s: float = 30.0
    stats_interval_s: float = 60.0
    trigger: TriggerConfig = field(default_factory=TriggerConfig)

    def __post_init__(self) -> None:
        for name in ("startup_timeout_s", "peer_max_age_s", "max_created_age_s",
                     "max_future_skew_s", "health_interval_s", "stats_interval_s"):
            if not getattr(self, name) > 0:
                raise ValueError(f"{name} must be > 0")
        if self.max_peers < 1 or self.seen_cache_size < 1 or not 0 <= self.poll_timeout_ms <= 30000:
            raise ValueError("max_peers/seen_cache_size >= 1 and 0 <= poll_timeout_ms <= 30000 required")


@dataclass(frozen=True)
class TickResult:
    local: dict[str, Any]
    regional: dict[str, Any] | None
    actions: list[Action]
    published: bool


# --------------------------------------------------------------------------- helpers
class PeerTable:
    """Latest local-scope state per peer. Ordered by (created_at, seq), thread-safe.

    created_at (peer clock) only orders states of the same peer, so a restarted peer
    (seq back to 0) still wins. If a peer's clock jumps backwards its entry would
    block it, so an entry older than ``max_age_s`` on OUR clock is replaced regardless.
    """

    def __init__(self, max_peers: int, max_age_s: float) -> None:
        self._max = max_peers
        self._max_age_s = max_age_s
        self._lock = threading.Lock()
        self._items: dict[str, PeerObservation] = {}

    def update(self, obs: PeerObservation) -> bool:
        with self._lock:
            cur = self._items.get(obs.peer_id)
            if cur is not None:
                cur_is_stale = (obs.received_at - cur.received_at).total_seconds() > self._max_age_s
                if not cur_is_stale and (obs.created_at, obs.seq) <= (cur.created_at, cur.seq):
                    return False
            elif len(self._items) >= self._max:
                oldest = min(self._items.values(), key=lambda o: o.received_at)
                del self._items[oldest.peer_id]
            self._items[obs.peer_id] = obs
            return True

    def snapshot(self, now: datetime) -> list[PeerObservation]:
        """Fresh observations, sorted by peer_id for deterministic output."""
        with self._lock:
            fresh = [o for o in self._items.values()
                     if (now - o.received_at).total_seconds() <= self._max_age_s]
        return sorted(fresh, key=lambda o: o.peer_id)

    def prune(self, now: datetime, factor: float = 10.0) -> int:
        with self._lock:
            dead = [k for k, o in self._items.items()
                    if (now - o.received_at).total_seconds() > self._max_age_s * factor]
            for key in dead:
                del self._items[key]
        return len(dead)

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)


class _SeenCache:
    """Bounded LRU of message_ids (cheap duplicate filter in front of the ledger)."""

    def __init__(self, size: int) -> None:
        self._size = size
        self._lock = threading.Lock()
        self._d: collections.OrderedDict[str, None] = collections.OrderedDict()

    def seen(self, key: str) -> bool:
        with self._lock:
            if key in self._d:
                self._d.move_to_end(key)
                return True
            return False

    def add(self, key: str) -> None:
        with self._lock:
            self._d[key] = None
            self._d.move_to_end(key)
            while len(self._d) > self._size:
                self._d.popitem(last=False)


class _Throttle:
    """Lets a hostile or broken peer produce at most one warning per key per interval."""

    def __init__(self, interval_s: float = 60.0) -> None:
        self._interval = interval_s
        self._lock = threading.Lock()
        self._last: dict[str, float] = {}

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            if len(self._last) > 4096:
                self._last.clear()
            if now - self._last.get(key, -1e18) >= self._interval:
                self._last[key] = now
                return True
            return False


_EOF = object()


class LineSource:
    """Iterates lines of a binary stream; yields None every ``poll_s`` of silence.

    A reader thread feeds a bounded queue, so the main loop can keep checking the
    stop flag and doing housekeeping while stdin is quiet.
    """

    def __init__(self, stream: BinaryIO, *, poll_s: float = 1.0, maxsize: int = 1000) -> None:
        self._q: queue.Queue[object] = queue.Queue(maxsize=maxsize)
        self._poll_s = poll_s
        threading.Thread(target=self._pump, args=(stream,), name="metrics-reader", daemon=True).start()

    def _pump(self, stream: BinaryIO) -> None:
        try:
            for line in stream:
                self._q.put(line)
        finally:
            self._q.put(_EOF)

    def __iter__(self) -> Iterator[bytes | None]:
        while True:
            try:
                item = self._q.get(timeout=self._poll_s)
            except queue.Empty:
                yield None
                continue
            if item is _EOF:
                return
            yield item  # type: ignore[misc]


# --------------------------------------------------------------------------- service
class Microservice:
    def __init__(
        self,
        config: ServiceConfig,
        *,
        bridge: BridgeClient | None,
        ledger: Ledger,
        inference: InferenceAdapter,
        triggers: TriggerEngine | None = None,
        handlers: Sequence[ActionHandler] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._cfg = config
        self._bridge = bridge
        self._ledger = ledger
        self._inference = inference
        self._triggers = triggers or TriggerEngine(config.trigger)
        self._handlers: list[ActionHandler] = list(handlers) if handlers is not None else [log_handler]
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._peers = PeerTable(config.max_peers, config.peer_max_age_s)
        self._seen = _SeenCache(config.seen_cache_size)
        self._throttle = _Throttle()
        self._stats: collections.Counter[str] = collections.Counter()
        self._stats_lock = threading.Lock()
        self._warned_node_id = False
        self.node_id: str | None = None
        self.fatal: str | None = None
        mono = time.monotonic()
        self._next_health = mono + config.health_interval_s
        self._next_stats = mono + config.stats_interval_s

    # ---- stats
    def _count(self, name: str, n: int = 1) -> None:
        with self._stats_lock:
            self._stats[name] += n

    def stats(self) -> dict[str, int]:
        with self._stats_lock:
            return dict(self._stats)

    # ---- identity
    def resolve_identity(self, stop: threading.Event | None = None) -> str:
        """node_id = the bridge's libp2p peer ID (what peers will verify)."""
        cfg = self._cfg
        if self._bridge is None:
            if not cfg.node_id:
                raise ServiceError("--node-id is required when running without a bridge")
            self.node_id = cfg.node_id
            return self.node_id
        if not self._bridge.wait_ready(cfg.startup_timeout_s, stop or threading.Event()):
            raise ServiceError(f"bridge at {self._bridge.base_url} not reachable within {cfg.startup_timeout_s}s")
        try:
            peer_id = self._bridge.health()["peer_id"]
        except BridgeError as exc:
            raise ServiceError(f"cannot read peer_id from the bridge: {exc}") from exc
        if cfg.node_id and cfg.node_id != peer_id:
            if cfg.enforce_peer_identity:
                raise ServiceError(
                    f"node_id {cfg.node_id!r} differs from the libp2p peer ID {peer_id!r}; peers would reject "
                    "our messages. Drop --node-id or use --no-peer-identity-check (dev only)."
                )
            self.node_id = cfg.node_id
        else:
            self.node_id = peer_id
        return self.node_id

    def _check_identity(self) -> None:
        """The Go node must keep its peer ID (use -key); if it changed we must restart."""
        if self._bridge is None or not self._cfg.enforce_peer_identity:
            return
        try:
            peer_id = self._bridge.health()["peer_id"]
        except BridgeError:
            return  # an outage is handled elsewhere (breaker, backoff)
        if peer_id != self.node_id:
            self.fatal = (f"bridge peer_id changed ({self.node_id} -> {peer_id}); peers would drop our messages. "
                          "Run the Go node with -key for a persistent identity. Exiting for restart.")

    # ---- ledger / publish
    def _record(self, entry_type: str, payload: Mapping[str, Any], origin: str, message_id: str | None) -> str:
        """Returns 'ok', 'duplicate' or 'error'. A ledger problem never stops the node."""
        try:
            self._ledger.append(entry_type, payload, origin_node_id=origin, message_id=message_id)
            return "ok"
        except DuplicateMessageError:
            return "duplicate"
        except LedgerError as exc:
            self._count("ledger_errors")
            log.error("ledger write failed (%s entry): %s", entry_type, exc)
            return "error"

    def _publish(self, raw: bytes) -> bool:
        """One attempt only: state is a heartbeat, the next one supersedes a retried stale one."""
        if self._bridge is None:
            return False
        try:
            self._bridge.publish(raw, attempts=1)
        except BridgeError as exc:
            self._count("publish_failed")
            if isinstance(exc, CircuitOpenError):
                log.debug("publish skipped: %s", exc)
            elif self._throttle.allow("publish_failed"):
                log.warning("publish failed: %s", exc)
            return False
        self._count("published")
        return True

    # ---- local pipeline
    def handle_metrics(self, raw: bytes | str | Mapping[str, Any]) -> TickResult | None:
        """One metrics message in; None when it was dropped (counted and logged)."""
        if self.node_id is None:
            raise ServiceError("resolve_identity() must be called first")
        self._count("metrics_in")
        try:
            obj = raw if isinstance(raw, Mapping) else decode_json(raw)
            validate_metrics(obj)
        except ContractError as exc:
            self._count("metrics_invalid")
            log.warning("dropping invalid metrics: %s", exc)
            return None

        metrics = dict(obj)
        if metrics["node_id"] != self.node_id:  # orchestration owns node identity
            if not self._warned_node_id:
                log.warning("metrics node_id %r replaced by this node's id %r", metrics["node_id"], self.node_id)
                self._warned_node_id = True
            metrics["node_id"] = self.node_id

        now = self._clock()
        try:
            local = dict(self._inference.local_state(metrics))
            validate_state(local)
            if local.get("scope") != "local":
                raise ContractError(f"local_state returned scope {local.get('scope')!r}")
            envelope = build_envelope(local, self.node_id, now=now)  # also checks node_id == origin
            wire = encode_envelope(envelope)
        except Exception:
            self._count("inference_error")
            log.exception("local state pipeline failed; metrics dropped")
            return None

        self._record("state", local, self.node_id, envelope["message_id"])  # record first, then broadcast
        published = self._publish(wire)
        regional, actions = self._regional_and_triggers(metrics, local, now)
        return TickResult(local, regional, actions, published)

    def _regional_and_triggers(self, metrics, local, now) -> tuple[dict[str, Any] | None, list[Action]]:
        regional: dict[str, Any] | None
        try:
            regional = dict(self._inference.regional_state(metrics, local, self._peers.snapshot(now), now=now))
            validate_state(regional)
            if regional.get("scope") != "regional" or regional.get("node_id") != self.node_id:
                raise ContractError("regional_state returned wrong scope or node_id")
        except Exception:
            regional = None
            self._count("regional_error")
            log.exception("regional estimate failed")
        else:
            self._record("state", regional, self.node_id, None)

        target = local if self._triggers.config.scope == "local" else regional
        if target is None:
            return regional, []
        try:
            actions = self._triggers.evaluate(target)
        except ValueError:
            self._count("trigger_error")
            log.exception("trigger evaluation failed")
            return regional, []
        for action in actions:
            self._count("actions_fired")
            self._record("action", action.to_payload(), self.node_id, None)  # intent is logged before acting
            failures = dispatch(action, self._handlers)
            if failures:
                self._count("action_handler_errors", failures)
        return regional, actions

    # ---- peer pipeline (runs in the bridge listener thread)
    def handle_peer_message(self, msg: BridgeMessage) -> str:
        """Outcome: accepted | stale | duplicate | rejected | non_local | self."""
        if self.node_id is None:
            raise ServiceError("resolve_identity() must be called first")
        cfg = self._cfg
        now = self._clock()
        try:
            env = parse_envelope(msg.data, now=now, max_future_skew_s=cfg.max_future_skew_s)
            if cfg.enforce_peer_identity:
                check_peer_identity(env, msg.from_peer)
        except ContractError as exc:
            name = type(exc).__name__
            self._count(f"peer_rejected_{name}")
            if self._throttle.allow(f"rej:{msg.from_peer}:{name}"):
                log.warning("rejected message from %s: %s", msg.from_peer, exc)
            return "rejected"

        if env.origin_node_id == self.node_id:
            self._count("peer_self")
            return "self"
        if env.payload.get("scope") != "local":  # regional states are never aggregated (echo loops)
            self._count("peer_non_local")
            if self._throttle.allow(f"scope:{env.origin_node_id}"):
                log.warning("dropping non-local state from %s", env.origin_node_id)
            return "non_local"
        if self._seen.seen(env.message_id):
            self._count("peer_duplicate")
            return "duplicate"

        outcome = self._record("state", dict(env.payload), env.origin_node_id, env.message_id)
        if outcome != "error":
            self._seen.add(env.message_id)
        if outcome == "duplicate":
            self._count("peer_duplicate")
            return "duplicate"

        # Replayed history (e.g. the bridge buffer after our restart) is audited but not used as live data.
        if (now - env.created_at).total_seconds() > cfg.max_created_age_s:
            self._count("peer_stale")
            return "stale"
        payload = env.payload
        self._peers.update(PeerObservation(
            peer_id=env.origin_node_id,
            congestion_score=payload["congestion_score"],
            confidence=payload["confidence"],
            seq=payload["seq"],
            created_at=env.created_at,
            received_at=now,
        ))
        self._count("peer_accepted")
        return "accepted"

    # ---- housekeeping
    def _housekeeping(self) -> None:
        mono = time.monotonic()
        if mono >= self._next_health:
            self._next_health = mono + self._cfg.health_interval_s
            self._check_identity()
        if mono >= self._next_stats:
            self._next_stats = mono + self._cfg.stats_interval_s
            self._log_stats()

    def _log_stats(self) -> None:
        self._peers.prune(self._clock())
        try:
            seq, head = self._ledger.head()
        except LedgerError:
            seq, head = -1, "?"
        log.info("stats %s peers=%d", self.stats(), len(self._peers))
        # Copy this line somewhere an attacker cannot reach: Ledger.verify_chain(anchor=(seq, hash)).
        log.info("ledger anchor seq=%d hash=%s", seq, head)

    # ---- main loop
    def run(self, source: Iterable[bytes | str | None], stop: threading.Event) -> int:
        try:
            self.resolve_identity(stop)
        except ServiceError as exc:
            log.error("startup failed: %s", exc)
            return 2
        log.info("node %s up (bridge=%s)", self.node_id, self._bridge.base_url if self._bridge else "none")
        listener: threading.Thread | None = None
        if self._bridge is not None:
            listener = threading.Thread(
                target=self._bridge.listen, args=(self.handle_peer_message, stop),
                kwargs={"timeout_ms": self._cfg.poll_timeout_ms}, name="bridge-listener", daemon=True)
            listener.start()
        try:
            for item in source:
                self._housekeeping()
                if stop.is_set() or self.fatal:
                    break
                if item is None or not item.strip():
                    continue
                try:
                    self.handle_metrics(item)
                except Exception:  # the loop must outlive any single message
                    self._count("loop_errors")
                    log.exception("unexpected error while handling metrics")
        finally:
            stop.set()
            if listener is not None:
                listener.join(timeout=2.0)
            self._log_stats()
        if self.fatal:
            log.critical(self.fatal)
            return 3
        return 0


# --------------------------------------------------------------------------- CLI
def build_parser() -> argparse.ArgumentParser:
    env = os.environ.get
    p = argparse.ArgumentParser(description="Traffic node orchestration microservice (reads metrics NDJSON on stdin)")
    p.add_argument("--ledger", default=env("LEDGER_PATH", "ledger.db"))
    p.add_argument("--bridge", default=None, help="bridge address (default: $BRIDGE_ADDR or 127.0.0.1:8080)")
    p.add_argument("--no-bridge", action="store_true", help="run alone: no broadcast, no peers (needs --node-id)")
    p.add_argument("--node-id", default=env("NODE_ID"))
    p.add_argument("--no-peer-identity-check", action="store_true", help="dev only")
    p.add_argument("--startup-timeout", type=float, default=30.0)
    p.add_argument("--peer-max-age", type=float, default=30.0)
    p.add_argument("--max-future-skew", type=float, default=30.0)
    p.add_argument("--enter-score", type=float, default=0.80)
    p.add_argument("--exit-score", type=float, default=0.70)
    p.add_argument("--min-confidence", type=float, default=0.65)
    p.add_argument("--enter-consecutive", type=int, default=2)
    p.add_argument("--exit-consecutive", type=int, default=2)
    p.add_argument("--cooldown", type=float, default=30.0)
    p.add_argument("--trigger-scope", choices=("local", "regional"), default=env("TRIGGER_SCOPE", "regional"))
    p.add_argument("--actions-file", default=env("ACTIONS_FILE"), help="append actions as JSON lines")
    p.add_argument("--log-level", default=env("LOG_LEVEL", "INFO"))
    return p


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(level=args.log_level.upper(), stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        trigger = TriggerConfig(
            enter_score=args.enter_score, exit_score=args.exit_score, min_confidence=args.min_confidence,
            enter_consecutive=args.enter_consecutive, exit_consecutive=args.exit_consecutive,
            cooldown_s=args.cooldown, scope=args.trigger_scope)
        config = ServiceConfig(
            ledger_path=args.ledger, node_id=args.node_id,
            enforce_peer_identity=not args.no_peer_identity_check, startup_timeout_s=args.startup_timeout,
            peer_max_age_s=args.peer_max_age, max_future_skew_s=args.max_future_skew, trigger=trigger)
    except ValueError as exc:
        parser.error(str(exc))
    try:
        inference = LilitInference(peer_max_age_s=config.peer_max_age_s)
    except InferenceUnavailableError as exc:
        log.error("%s", exc)
        return 2

    handlers: list[ActionHandler] = [log_handler]
    if args.actions_file:
        handlers.append(JsonlActionSink(args.actions_file))

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    with Ledger(config.ledger_path) as ledger:
        bridge = None if args.no_bridge else BridgeClient(args.bridge)
        service = Microservice(config, bridge=bridge, ledger=ledger, inference=inference, handlers=handlers)
        return service.run(LineSource(sys.stdin.buffer), stop)


if __name__ == "__main__":
    raise SystemExit(main())