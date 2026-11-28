"""Unit tests for src/serializer.py."""
from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone

import pytest

from src import serializer as s

NOW = datetime(2026, 11, 20, 10, 0, 10, 500000, tzinfo=timezone.utc)


def load_example(name: str) -> dict:
    return json.loads((s.contracts_dir() / "examples" / name).read_text(encoding="utf-8"))


@pytest.fixture
def metrics() -> dict:
    return load_example("metrics.example.json")


@pytest.fixture
def state() -> dict:
    return load_example("state.example.json")


# ----------------------------------------------------------------- examples stay valid
def test_examples_validate(metrics, state):
    s.validate_metrics(metrics)
    s.validate_state(state)
    env = s.parse_envelope((s.contracts_dir() / "examples" / "envelope.example.json").read_bytes())
    assert env.payload_type == "state"


# ----------------------------------------------------------------- versioning
@pytest.mark.parametrize("bad", ["1.0.0", "0.1.0", "0.3.0"])
def test_incompatible_version(state, bad):
    state["schema_version"] = bad
    with pytest.raises(s.UnsupportedVersionError):
        s.validate_state(state)


def test_compatible_patch_version(state):
    state["schema_version"] = f"{s.SUPPORTED_SCHEMA_VERSION[:-1]}7"
    s.validate_state(state)


def test_missing_version(state):
    del state["schema_version"]
    with pytest.raises(s.SchemaValidationError, match="schema_version"):
        s.validate_state(state)


def test_non_object_root():
    with pytest.raises(s.SchemaValidationError, match="expected a JSON object"):
        s.validate_state([1, 2])


# ----------------------------------------------------------------- state rules
def test_unknown_field_rejected(state):
    state["extra"] = 1
    with pytest.raises(s.SchemaValidationError, match="extra"):
        s.validate_state(state)


def test_unknown_state_label_rejected(state):
    state["state"] = "jammed"
    with pytest.raises(s.SchemaValidationError, match="state"):
        s.validate_state(state)


@pytest.mark.parametrize("field", ["congestion_score", "confidence"])
@pytest.mark.parametrize("value", [-0.01, 1.01])
def test_score_range(state, field, value):
    state[field] = value
    with pytest.raises(s.SchemaValidationError, match=field):
        s.validate_state(state)


@pytest.mark.parametrize("value", [0.0, 1.0])
def test_score_boundaries_accepted(state, value):
    state["congestion_score"] = value
    s.validate_state(state)


def test_bool_is_not_an_integer(state):
    state["seq"] = True
    with pytest.raises(s.SchemaValidationError, match="seq"):
        s.validate_state(state)


def test_local_scope_must_not_list_peers(state):
    state["contributing_peers"] = ["node-b"]
    with pytest.raises(s.SchemaValidationError, match="contributing_peers"):
        s.validate_state(state)


def test_regional_scope_accepts_peers(state):
    state.update(scope="regional", contributing_peers=["node-b", "node-c"])
    s.validate_state(state)


def test_duplicate_peers_rejected(state):
    state.update(scope="regional", contributing_peers=["node-b", "node-b"])
    with pytest.raises(s.SchemaValidationError):
        s.validate_state(state)


@pytest.mark.parametrize("ts", ["2026-11-20T10:00:10Z", "2026-11-20 10:00:10.250Z", "2026-11-20T10:00:10.250+00:00"])
def test_timestamp_format_strict(state, ts):
    state["timestamp"] = ts
    with pytest.raises(s.SchemaValidationError, match="timestamp"):
        s.validate_state(state)


def test_errors_are_aggregated(state):
    state["state"] = "nope"
    state["confidence"] = 5
    with pytest.raises(s.SchemaValidationError) as exc:
        s.validate_state(state)
    assert len(exc.value.errors) == 2


# ----------------------------------------------------------------- metrics rules
def test_metrics_count_must_match_classes(metrics):
    metrics["lanes"][0]["vehicle_count"] = 99
    with pytest.raises(s.SchemaValidationError, match="sum of counts_by_class"):
        s.validate_metrics(metrics)


def test_metrics_interval_order(metrics):
    metrics["interval_end"] = metrics["interval_start"]
    with pytest.raises(s.SchemaValidationError, match="after interval_start"):
        s.validate_metrics(metrics)


def test_metrics_duplicate_lane(metrics):
    metrics["lanes"][1]["lane_id"] = metrics["lanes"][0]["lane_id"]
    with pytest.raises(s.SchemaValidationError, match="duplicate"):
        s.validate_metrics(metrics)


def test_metrics_occupancy_range(metrics):
    metrics["lanes"][0]["avg_occupancy"] = 1.5
    with pytest.raises(s.SchemaValidationError, match="avg_occupancy"):
        s.validate_metrics(metrics)


YOLO_CLASSES = ("car", "motorcycle", "bus", "truck")  # COCO ids 2, 3, 5, 7


def test_metrics_classes_are_the_yolo_vehicle_classes():
    schema = json.loads((s.contracts_dir() / "metrics_schema.json").read_text())
    counts = schema["properties"]["lanes"]["items"]["properties"]["counts_by_class"]
    assert tuple(counts["required"]) == YOLO_CLASSES
    assert set(counts["properties"]) == set(YOLO_CLASSES)


def test_metrics_legacy_classes_rejected(metrics):
    metrics["lanes"][0]["counts_by_class"] = {"emergency": 0, "public": 1, "private": 3}
    with pytest.raises(s.SchemaValidationError) as exc:
        s.validate_metrics(metrics)
    assert "counts_by_class" in str(exc.value)


def test_metrics_missing_class_rejected(metrics):
    del metrics["lanes"][0]["counts_by_class"]["truck"]
    with pytest.raises(s.SchemaValidationError, match="truck"):
        s.validate_metrics(metrics)


def test_metrics_unknown_class_rejected(metrics):
    metrics["lanes"][0]["counts_by_class"]["bicycle"] = 0  # not in the contract
    with pytest.raises(s.SchemaValidationError, match="bicycle"):
        s.validate_metrics(metrics)


def test_metrics_negative_class_count_rejected(metrics):
    metrics["lanes"][0]["counts_by_class"]["car"] = -1
    with pytest.raises(s.SchemaValidationError, match="car"):
        s.validate_metrics(metrics)


def test_metrics_all_classes_contribute_to_total(metrics):
    metrics["lanes"][0]["counts_by_class"] = {"car": 1, "motorcycle": 1, "bus": 1, "truck": 1}
    metrics["lanes"][0]["vehicle_count"] = 4
    s.validate_metrics(metrics)
    metrics["lanes"][0]["vehicle_count"] = 3
    with pytest.raises(s.SchemaValidationError, match="sum of counts_by_class"):
        s.validate_metrics(metrics)


def test_metrics_empty_lanes(metrics):
    metrics["lanes"] = []
    with pytest.raises(s.SchemaValidationError, match="lanes"):
        s.validate_metrics(metrics)


# ----------------------------------------------------------------- decoding / canonical form
def test_malformed_json():
    with pytest.raises(s.MalformedMessageError):
        s.decode_json(b"{not json")


def test_invalid_utf8():
    with pytest.raises(s.MalformedMessageError):
        s.decode_json(b"\xff\xfe")


def test_nan_rejected():
    with pytest.raises(s.MalformedMessageError):
        s.decode_json('{"congestion_score": NaN}')


def test_partial_payload_rejected(state):
    raw = json.dumps(state).encode()[:-10]
    with pytest.raises(s.MalformedMessageError):
        s.decode_json(raw)


def test_canonical_json_ignores_key_order():
    assert s.canonical_json({"b": 1, "a": 2}) == s.canonical_json({"a": 2, "b": 1})


def test_utc_now_iso_millis():
    assert s.utc_now_iso(NOW) == "2026-11-20T10:00:10.500Z"
    assert s.parse_timestamp("2026-11-20T10:00:10.500Z") == NOW


# ----------------------------------------------------------------- envelope
def test_roundtrip(state):
    raw = s.encode_envelope(s.build_envelope(state, "node-a", now=NOW))
    env = s.parse_envelope(raw)
    assert env.payload == state
    assert env.origin_node_id == "node-a"
    assert env.created_at == NOW


def test_build_rejects_invalid_state(state):
    state["state"] = "bad"
    with pytest.raises(s.SchemaValidationError):
        s.build_envelope(state, "node-a")


def test_build_rejects_origin_mismatch(state):
    with pytest.raises(s.IntegrityError):
        s.build_envelope(state, "node-z")


def test_tampered_payload_detected(state):
    env = s.build_envelope(state, "node-a", now=NOW)
    env["payload"] = copy.deepcopy(env["payload"])
    env["payload"]["congestion_score"] = 0.99
    with pytest.raises(s.IntegrityError, match="payload_sha256"):
        s.parse_envelope(json.dumps(env))


def test_spoofed_origin_detected(state):
    env = s.build_envelope(state, "node-a", now=NOW)
    env["origin_node_id"] = "node-evil"
    with pytest.raises(s.IntegrityError, match="origin_node_id"):
        s.parse_envelope(json.dumps(env))


def test_bad_inner_payload_detected(state):
    env = s.build_envelope(state, "node-a", now=NOW)
    env["payload"]["state"] = "jammed"
    env["payload_sha256"] = s.payload_digest(env["payload"])  # attacker fixes the hash
    with pytest.raises(s.SchemaValidationError):
        s.parse_envelope(json.dumps(env))


def test_future_skew_rejected(state):
    env = s.build_envelope(state, "node-a", now=NOW + timedelta(seconds=30))
    raw = s.encode_envelope(env)
    with pytest.raises(s.ClockSkewError):
        s.parse_envelope(raw, now=NOW, max_future_skew_s=5)
    s.parse_envelope(raw, now=NOW, max_future_skew_s=60)  # within tolerance
    s.parse_envelope(raw)  # check disabled


def test_old_message_not_rejected_by_skew(state):
    env = s.build_envelope(state, "node-a", now=NOW - timedelta(hours=1))
    s.parse_envelope(s.encode_envelope(env), now=NOW, max_future_skew_s=5)


def test_unique_message_ids(state):
    ids = {s.build_envelope(state, "node-a")["message_id"] for _ in range(50)}
    assert len(ids) == 50


# ----------------------------------------------------------------- impossible calendar timestamps
BAD_CALENDAR = ["2026-13-45T25:61:61.000Z", "2026-02-30T10:00:00.000Z", "0000-01-01T00:00:00.000Z", "2026-06-30T23:59:60.000Z"]


@pytest.mark.parametrize("ts", BAD_CALENDAR)
def test_state_rejects_impossible_timestamp(state, ts):
    state["timestamp"] = ts
    with pytest.raises(s.SchemaValidationError, match="timestamp"):
        s.validate_state(state)


@pytest.mark.parametrize("field", ["interval_start", "interval_end"])
@pytest.mark.parametrize("ts", BAD_CALENDAR)
def test_metrics_rejects_impossible_timestamp(metrics, field, ts):
    metrics[field] = ts
    with pytest.raises(s.SchemaValidationError, match=field):  # a ContractError, never a bare ValueError
        s.validate_metrics(metrics)


@pytest.mark.parametrize("ts", BAD_CALENDAR)
def test_envelope_rejects_impossible_timestamp(state, ts):
    env = s.build_envelope(state, "node-a", now=NOW)
    env["created_at"] = ts
    with pytest.raises(s.SchemaValidationError, match="created_at"):
        s.parse_envelope(json.dumps(env))


# ----------------------------------------------------------------- hostile peer input: only ContractError may escape
HOSTILE = {
    "empty": b"",
    "null": b"null",
    "array": b"[]",
    "number": b"123",
    "string": b'"text"',
    "empty_object": b"{}",
    "bad_utf8": b"\xff\xfe\x00",
    "truncated": b'{"schema_version": "0.2.0", "message_id":',
    "deep_nesting": b"[" * 30000 + b"]" * 30000,
    "huge_integer": b'{"seq": ' + b"9" * 5000 + b"}",
    "nan": b'{"payload": NaN}',
    "oversize": b'{"x": "' + b"a" * s.MAX_ENVELOPE_BYTES + b'"}',
}


@pytest.mark.parametrize("raw", HOSTILE.values(), ids=HOSTILE.keys())
def test_parse_envelope_only_raises_contract_errors(raw):
    with pytest.raises(s.ContractError):
        s.parse_envelope(raw)


@pytest.mark.parametrize("raw", [b"[" * 100000 + b"]" * 100000, b'{"a":' + b"9" * 5000 + b"}", None, 5])
def test_decode_json_only_raises_malformed(raw):
    with pytest.raises(s.MalformedMessageError):
        s.decode_json(raw)


def test_deeply_nested_payload_does_not_crash_validation(state):
    state["contributing_peers"] = [[[[]]]]
    with pytest.raises(s.SchemaValidationError):
        s.validate_state(state)


def test_envelope_size_limit_applies_to_str_too(state):
    env = s.build_envelope(state, "node-a", now=NOW)
    env["padding"] = "a" * s.MAX_ENVELOPE_BYTES
    with pytest.raises(s.MalformedMessageError, match="limit"):
        s.parse_envelope(json.dumps(env))


def test_envelope_at_size_limit_boundary_is_not_a_size_error(state):
    raw = s.encode_envelope(s.build_envelope(state, "node-a", now=NOW))
    assert len(raw) < s.MAX_ENVELOPE_BYTES
    s.parse_envelope(raw)


# ----------------------------------------------------------------- payload_type handling
def test_non_state_payload_type_rejected_by_schema(state):
    env = s.build_envelope(state, "node-a", now=NOW)
    env["payload_type"] = "metrics"
    with pytest.raises(s.SchemaValidationError, match="payload_type"):
        s.parse_envelope(json.dumps(env))


def test_unknown_payload_type_is_handled_safely_even_past_the_schema():
    with pytest.raises(s.SchemaValidationError, match="unsupported"):
        s._validate_payload("bogus", {"node_id": "node-a"})


def test_registered_payload_type_must_provide_node_id(monkeypatch):
    monkeypatch.setitem(s._PAYLOAD_VALIDATORS, "future", lambda payload: payload)
    with pytest.raises(s.SchemaValidationError, match="node_id"):
        s._validate_payload("future", {"no_node_id_here": 1})  # no KeyError
    with pytest.raises(s.SchemaValidationError, match="node_id"):
        s._validate_payload("future", {"node_id": 7})
    assert s._validate_payload("future", {"node_id": "node-x"}) == "node-x"


# ----------------------------------------------------------------- peer identity (ingestion layer helper)
def test_peer_identity_match(state):
    env = s.parse_envelope(s.encode_envelope(s.build_envelope(state, "node-a", now=NOW)))
    s.check_peer_identity(env, "node-a")


def test_peer_identity_mismatch_rejected(state):
    env = s.parse_envelope(s.encode_envelope(s.build_envelope(state, "node-a", now=NOW)))
    with pytest.raises(s.IntegrityError, match="authoring peer"):
        s.check_peer_identity(env, "12D3KooWSomeoneElse")


# ----------------------------------------------------------------- NaN / Infinity in in-process dicts
@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
@pytest.mark.parametrize("field", ["congestion_score", "confidence"])
def test_state_rejects_non_finite_numbers(state, field, bad):
    state[field] = bad
    with pytest.raises(s.SchemaValidationError, match=field):
        s.validate_state(state)


@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
@pytest.mark.parametrize("field", ["avg_occupancy", "flow_rate_vph"])
def test_metrics_reject_non_finite_numbers(metrics, field, bad):
    metrics["lanes"][1][field] = bad
    with pytest.raises(s.SchemaValidationError, match=f"lanes/1/{field}"):
        s.validate_metrics(metrics)


def test_build_envelope_raises_a_contract_error_for_nan(state):
    state["congestion_score"] = float("nan")
    with pytest.raises(s.ContractError):  # used to escape as a bare ValueError
        s.build_envelope(state, "node-a")


def test_envelope_payload_nan_is_reported_with_its_path(state):
    env = s.build_envelope(state, "node-a", now=NOW)
    env["payload"]["congestion_score"] = float("nan")
    with pytest.raises(s.SchemaValidationError, match="payload/congestion_score"):
        s.validate_envelope(env)


def test_all_non_finite_fields_are_listed_together(state):
    state["congestion_score"] = float("nan")
    state["confidence"] = float("inf")
    with pytest.raises(s.SchemaValidationError) as exc:
        s.validate_state(state)
    assert len(exc.value.errors) == 2