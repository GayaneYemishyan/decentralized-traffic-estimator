#!/usr/bin/env python3
"""CI-1: contracts/examples/*.json must satisfy their schemas and the documented semantic rules."""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "orchestration"))

from jsonschema import Draft202012Validator  # noqa: E402

from src import serializer as s  # noqa: E402

CASES = {  # example file -> (schema name, validator)
    "metrics.example.json": ("metrics", s.validate_metrics),
    "state.example.json": ("state", s.validate_state),
    "envelope.example.json": ("envelope", lambda o: s.parse_envelope(json.dumps(o))),
}


def must_fail(label: str, fn, obj, errors: list[str]) -> None:
    try:
        fn(obj)
    except s.ContractError:
        return
    errors.append(f"{label}: a deliberately broken example was ACCEPTED (schema too loose?)")


def main() -> int:
    cdir, errors = s.contracts_dir(), []
    for kind in ("metrics", "state", "envelope"):
        try:
            Draft202012Validator.check_schema(json.loads((cdir / f"{kind}_schema.json").read_text("utf-8")))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{kind}_schema.json: invalid schema: {exc}")

    examples = {p.name: json.loads(p.read_text("utf-8")) for p in sorted((cdir / "examples").glob("*.json"))}
    for name in examples.keys() - CASES.keys():
        errors.append(f"examples/{name}: no schema mapped in scripts/validate_examples.py")
    for name, (kind, validate) in CASES.items():
        obj = examples.get(name)
        if obj is None:
            errors.append(f"examples/{name}: missing")
            continue
        try:
            validate(obj)
        except s.ContractError as exc:
            errors.append(f"{name}: {exc}")
        version = obj.get("schema_version")
        if version != s.SUPPORTED_SCHEMA_VERSION:
            errors.append(f"{name}: schema_version {version!r} != {s.SUPPORTED_SCHEMA_VERSION!r}")

    if not errors:  # negative controls
        bad = copy.deepcopy(examples["state.example.json"]); bad["state"] = "jammed"
        must_fail("state label", s.validate_state, bad, errors)
        bad = copy.deepcopy(examples["metrics.example.json"]); bad["lanes"][0]["counts_by_class"] = {"emergency": 4}
        must_fail("legacy classes", s.validate_metrics, bad, errors)
        bad = copy.deepcopy(examples["envelope.example.json"]); bad["payload"]["congestion_score"] = 0.01
        must_fail("tampered envelope", lambda o: s.parse_envelope(json.dumps(o)), bad, errors)

    for e in errors:
        print(f"FAIL  {e}")
    print("OK: all contract examples valid" if not errors else f"\n{len(errors)} problem(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())