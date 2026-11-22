"""Schema validation and (de)serialization for the traffic-estimator contracts.

Single entry point for every message crossing a module boundary on P4's side.
Design rules:
  * Fail fast, with every violation listed in the error message.
  * Check ``schema_version`` *before* structural validation so version drift
    produces a clear error instead of a wall of field errors.
  * The envelope's ``payload_sha256`` is computed over canonical JSON, so the
    same payload always hashes the same on every node.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

from jsonschema import Draft202012Validator

SUPPORTED_SCHEMA_VERSION = "0.1.0"
_SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
_TS_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"
_KINDS = ("metrics", "state", "envelope")


# --------------------------------------------------------------------------- errors
class ContractError(Exception):
    """Base class for all contract violations."""


class MalformedMessageError(ContractError):
    """Bytes are not valid UTF-8 JSON (or contain NaN/Infinity)."""


class SchemaValidationError(ContractError):
    """Message does not satisfy its JSON schema or a documented semantic rule."""

    def __init__(self, kind: str, errors: list[str]) -> None:
        self.kind = kind
        self.errors = errors
        super().__init__(f"{kind} message invalid: " + "; ".join(errors))


class UnsupportedVersionError(ContractError):
    """schema_version is not compatible with this node."""

    def __init__(self, kind: str, version: str) -> None:
        self.kind = kind
        self.version = version
        super().__init__(
            f"{kind}: schema_version {version!r} not supported "
            f"(this node speaks {SUPPORTED_SCHEMA_VERSION})"
        )


class IntegrityError(ContractError):
    """Digest mismatch or origin spoofing."""


class ClockSkewError(ContractError):
    """Envelope is timestamped further in the future than tolerated."""


# --------------------------------------------------------------------------- helpers
def contracts_dir() -> Path:
    """Locate ``contracts/`` (override with the CONTRACTS_DIR env var, e.g. in Docker)."""
    env = os.environ.get("CONTRACTS_DIR")
    return Path(env) if env else Path(__file__).resolve().parents[2] / "contracts"


@lru_cache(maxsize=None)
def _validator(kind: str) -> Draft202012Validator:
    path = contracts_dir() / f"{kind}_schema.json"
    schema = json.loads(path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def _reject_constant(name: str) -> Any:
    raise MalformedMessageError(f"non-finite number {name} is not allowed")


def decode_json(raw: bytes | str) -> Any:
    """Strict JSON decode: UTF-8 only, no NaN/Infinity."""
    try:
        text = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw
        return json.loads(text, parse_constant=_reject_constant)
    except MalformedMessageError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MalformedMessageError(f"invalid JSON: {exc}") from exc


def canonical_json(obj: Any) -> bytes:
    """Deterministic encoding: sorted keys, compact separators, UTF-8, no NaN."""
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def payload_digest(payload: Mapping[str, Any]) -> str:
    """Hex SHA-256 of the canonical JSON of ``payload``."""
    return hashlib.sha256(canonical_json(payload)).hexdigest()


def utc_now_iso(now: datetime | None = None) -> str:
    """UTC timestamp with exact millisecond precision, e.g. 2026-11-20T10:00:00.000Z."""
    dt = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def parse_timestamp(value: str) -> datetime:
    """Parse a contract timestamp into an aware UTC datetime."""
    return datetime.strptime(value, _TS_FORMAT).replace(tzinfo=timezone.utc)


def _is_compatible(version: str) -> bool:
    got = tuple(int(p) for p in _SEMVER.match(version).groups())  # type: ignore[union-attr]
    want = tuple(int(p) for p in _SEMVER.match(SUPPORTED_SCHEMA_VERSION).groups())  # type: ignore[union-attr]
    if got[0] != want[0]:
        return False
    return got[0] > 0 or got[1] == want[1]  # 0.x: minor bumps are breaking


def _check_version(kind: str, obj: Any) -> None:
    if not isinstance(obj, Mapping):
        raise SchemaValidationError(kind, ["<root>: expected a JSON object"])
    version = obj.get("schema_version")
    if not isinstance(version, str) or not _SEMVER.match(version):
        raise SchemaValidationError(kind, [f"schema_version: missing or not semver (got {version!r})"])
    if not _is_compatible(version):
        raise UnsupportedVersionError(kind, version)


def _validate(kind: str, obj: Any) -> None:
    _check_version(kind, obj)
    errors = sorted(_validator(kind).iter_errors(obj), key=lambda e: [str(p) for p in e.absolute_path])
    if errors:
        msgs = ["/".join(str(p) for p in e.absolute_path) or "<root>" for e in errors]
        raise SchemaValidationError(kind, [f"{loc}: {e.message}" for loc, e in zip(msgs, errors)])


# --------------------------------------------------------------------------- validators
def validate_metrics(obj: Any) -> Mapping[str, Any]:
    """Validate a metrics message (P1 -> P2), including cross-field rules."""
    _validate("metrics", obj)
    errs: list[str] = []
    if parse_timestamp(obj["interval_end"]) <= parse_timestamp(obj["interval_start"]):
        errs.append("interval_end: must be after interval_start")
    seen: set[str] = set()
    for i, lane in enumerate(obj["lanes"]):
        if lane["lane_id"] in seen:
            errs.append(f"lanes/{i}/lane_id: duplicate {lane['lane_id']!r}")
        seen.add(lane["lane_id"])
        if lane["vehicle_count"] != sum(lane["counts_by_class"].values()):
            errs.append(f"lanes/{i}/vehicle_count: must equal sum of counts_by_class")
    if errs:
        raise SchemaValidationError("metrics", errs)
    return obj


def validate_state(obj: Any) -> Mapping[str, Any]:
    """Validate a state message (P2 -> P3/P4)."""
    _validate("state", obj)
    return obj


def validate_envelope(obj: Any) -> Mapping[str, Any]:
    """Structural validation only. Use :func:`parse_envelope` for digest checks."""
    _validate("envelope", obj)
    return obj


# --------------------------------------------------------------------------- envelope
@dataclass(frozen=True)
class Envelope:
    """A verified envelope. Constructed only by :func:`parse_envelope`."""

    schema_version: str
    message_id: str
    origin_node_id: str
    created_at: datetime
    payload_type: str
    payload: Mapping[str, Any]
    payload_sha256: str


def build_envelope(
    state: Mapping[str, Any],
    origin_node_id: str,
    *,
    message_id: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Wrap a validated state message for broadcast to peers."""
    validate_state(state)
    if state["node_id"] != origin_node_id:
        raise IntegrityError(
            f"origin_node_id {origin_node_id!r} != state.node_id {state['node_id']!r}"
        )
    env = {
        "schema_version": SUPPORTED_SCHEMA_VERSION,
        "message_id": message_id or str(uuid.uuid4()),
        "origin_node_id": origin_node_id,
        "created_at": utc_now_iso(now),
        "payload_type": "state",
        "payload": dict(state),
        "payload_sha256": payload_digest(state),
    }
    validate_envelope(env)
    return env


def encode_envelope(envelope: Mapping[str, Any]) -> bytes:
    """Serialize an envelope to the exact bytes handed to the bridge."""
    validate_envelope(envelope)
    return canonical_json(envelope)


def parse_envelope(
    raw: bytes | str,
    *,
    now: datetime | None = None,
    max_future_skew_s: float | None = None,
) -> Envelope:
    """Decode and fully verify a peer envelope.

    Order: JSON -> version -> schema -> digest -> inner payload -> origin ->
    clock skew. Only *future* skew is rejected: delayed delivery is normal in
    gossip, so staleness is the caller's policy (seq / age checks).
    """
    obj = decode_json(raw)
    validate_envelope(obj)
    if not hmac.compare_digest(payload_digest(obj["payload"]), obj["payload_sha256"]):
        raise IntegrityError("payload_sha256 does not match payload")
    if obj["payload_type"] == "state":
        validate_state(obj["payload"])
    if obj["payload"]["node_id"] != obj["origin_node_id"]:
        raise IntegrityError("origin_node_id does not match payload.node_id")
    created = parse_timestamp(obj["created_at"])
    if max_future_skew_s is not None:
        ref = now or datetime.now(timezone.utc)
        if created - ref > timedelta(seconds=max_future_skew_s):
            raise ClockSkewError(
                f"created_at {obj['created_at']} is more than {max_future_skew_s}s in the future"
            )
    return Envelope(
        schema_version=obj["schema_version"],
        message_id=obj["message_id"],
        origin_node_id=obj["origin_node_id"],
        created_at=created,
        payload_type=obj["payload_type"],
        payload=obj["payload"],
        payload_sha256=obj["payload_sha256"],
    )
