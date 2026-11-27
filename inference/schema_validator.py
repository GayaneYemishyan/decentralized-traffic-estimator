from __future__ import annotations

import json
import math
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Optional


class SchemaValidationError(ValueError):
    """Raised when a metrics or state payload violates its contract schema."""


_SUPPORTED_KEYWORDS: FrozenSet[str] = frozenset({
    "$schema",
    "$id",
    "$defs",
    "definitions",
    "title",
    "description",
    "$ref",
    "const",
    "enum",
    "type",
    "minLength",
    "maxLength",
    "pattern",
    "format",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "minItems",
    "maxItems",
    "uniqueItems",
    "items",
    "required",
    "properties",
    "additionalProperties",
    "if",
    "then",
    "else",
})


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def load_schema(schema_name: str, contracts_dir: Optional[Path] = None) -> Dict[str, Any]:
    base = contracts_dir if contracts_dir is not None else (_repo_root() / "contracts")
    schema_path = base / schema_name
    if not schema_path.exists():
        raise FileNotFoundError(f"Schema file not found: {schema_path}")
    with schema_path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _resolve_ref(ref: str, root_schema: Dict[str, Any]) -> Dict[str, Any]:
    if not ref.startswith("#/"):
        raise SchemaValidationError(f"Unsupported $ref target: {ref!r}")
    current: Any = root_schema
    for part in ref[2:].split("/"):
        unescaped = part.replace("~1", "/").replace("~0", "~")
        if not isinstance(current, dict) or unescaped not in current:
            raise SchemaValidationError(f"Unresolvable $ref target: {ref!r}")
        current = current[unescaped]
    if not isinstance(current, dict):
        raise SchemaValidationError(f"Resolved $ref is not an object: {ref!r}")
    return current


def _is_valid_datetime(value: str) -> bool:
    if "T" not in value and "t" not in value:
        return False
    normalized = value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _check_type(value: Any, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return False
        return math.isfinite(float(value))
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    return False


def _reject_unsupported_keywords(schema: Dict[str, Any], path: str) -> None:
    unsupported = sorted(k for k in schema.keys() if k not in _SUPPORTED_KEYWORDS)
    if unsupported:
        raise SchemaValidationError(
            f"{path}: unsupported schema keyword(s) in fallback validator: {', '.join(unsupported)}"
        )


def _validate_node(
    value: Any,
    schema: Dict[str, Any],
    root_schema: Dict[str, Any],
    path: str,
    errors: List[str],
) -> None:
    _reject_unsupported_keywords(schema, path)

    if "$ref" in schema:
        target = _resolve_ref(schema["$ref"], root_schema)
        merged = {k: v for k, v in schema.items() if k != "$ref"}
        effective = {**target, **merged}
        _validate_node(value, effective, root_schema, path, errors)
        return

    if "const" in schema and value != schema["const"]:
        errors.append(f"{path}: expected const {schema['const']!r}, got {value!r}")
        return

    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: value {value!r} not in allowed enum {schema['enum']!r}")
        return

    expected_type = schema.get("type")
    if expected_type is not None:
        if not _check_type(value, expected_type):
            errors.append(f"{path}: expected type '{expected_type}', got {type(value).__name__}")
            return

    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            errors.append(f"{path}: length {len(value)} < minLength {schema['minLength']}")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append(f"{path}: length {len(value)} > maxLength {schema['maxLength']}")
        if "pattern" in schema and re.search(schema["pattern"], value) is None:
            errors.append(f"{path}: value {value!r} does not match pattern {schema['pattern']!r}")
        if schema.get("format") == "date-time" and not _is_valid_datetime(value):
            errors.append(f"{path}: timestamp {value!r} is not a valid ISO-8601 date-time with timezone")

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if not math.isfinite(float(value)):
            errors.append(f"{path}: numeric value {value!r} must be finite")
            return
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: value {value} < minimum {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: value {value} > maximum {schema['maximum']}")
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            errors.append(f"{path}: value {value} <= exclusiveMinimum {schema['exclusiveMinimum']}")
        if "exclusiveMaximum" in schema and value >= schema["exclusiveMaximum"]:
            errors.append(f"{path}: value {value} >= exclusiveMaximum {schema['exclusiveMaximum']}")

    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append(f"{path}: item count {len(value)} < minItems {schema['minItems']}")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(f"{path}: item count {len(value)} > maxItems {schema['maxItems']}")
        if schema.get("uniqueItems"):
            seen: List[Any] = []
            for item in value:
                if item in seen:
                    errors.append(f"{path}: duplicate item {item!r} violates uniqueItems")
                    break
                seen.append(item)
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for idx, item in enumerate(value):
                _validate_node(item, item_schema, root_schema, f"{path}[{idx}]", errors)

    if isinstance(value, dict):
        required = schema.get("required", [])
        for req_key in required:
            if req_key not in value:
                errors.append(f"{path}: missing required field '{req_key}'")

        properties = schema.get("properties", {})
        additional_allowed = schema.get("additionalProperties", True)
        if additional_allowed is False:
            for key in value:
                if key not in properties:
                    errors.append(f"{path}: unexpected field '{key}'")

        for key, prop_schema in properties.items():
            if key in value and isinstance(prop_schema, dict):
                _validate_node(value[key], prop_schema, root_schema, f"{path}.{key}", errors)

    if "if" in schema and isinstance(schema["if"], dict):
        if_errors: List[str] = []
        _validate_node(value, schema["if"], root_schema, path, if_errors)
        if not if_errors and "then" in schema and isinstance(schema["then"], dict):
            _validate_node(value, schema["then"], root_schema, path, errors)
        elif if_errors and "else" in schema and isinstance(schema["else"], dict):
            _validate_node(value, schema["else"], root_schema, path, errors)


def validate_payload(
    payload: Dict[str, Any],
    schema_name: str,
    contracts_dir: Optional[Path] = None,
) -> None:
    """Validate a payload dictionary against a contract JSON schema."""
    schema = load_schema(schema_name, contracts_dir=contracts_dir)

    try:
        import jsonschema  # type: ignore
    except ImportError:
        errors: List[str] = []
        _validate_node(payload, schema, schema, "$", errors)
        if errors:
            raise SchemaValidationError(f"Invalid {schema_name}: " + "; ".join(errors))
        return

    validator_cls = jsonschema.validators.validator_for(schema)
    format_checker = getattr(validator_cls, "FORMAT_CHECKER", None)
    validator = validator_cls(schema, format_checker=format_checker)
    validation_errors = sorted(validator.iter_errors(payload), key=lambda err: list(err.path))
    if validation_errors:
        formatted = "; ".join(
            f"{'.'.join(str(p) for p in err.path) or '$'}: {err.message}"
            for err in validation_errors
        )
        raise SchemaValidationError(f"Invalid {schema_name}: {formatted}")


def validate_metrics_payload(payload: Dict[str, Any], contracts_dir: Optional[Path] = None) -> None:
    validate_payload(payload, "metrics_schema.json", contracts_dir=contracts_dir)
    lanes = payload.get("lanes")
    if isinstance(lanes, list):
        mismatches: List[str] = []
        for idx, lane in enumerate(lanes):
            if isinstance(lane, dict):
                counts = lane.get("counts_by_class")
                vehicle_count = lane.get("vehicle_count")
                if isinstance(counts, dict) and isinstance(vehicle_count, int) and not isinstance(vehicle_count, bool):
                    expected_sum = sum(
                        v for v in counts.values() if isinstance(v, int) and not isinstance(v, bool)
                    )
                    if vehicle_count != expected_sum:
                        mismatches.append(
                            f"$.lanes[{idx}].vehicle_count ({vehicle_count}) != sum(counts_by_class) ({expected_sum})"
                        )
        if mismatches:
            raise SchemaValidationError("Invalid metrics_schema.json: " + "; ".join(mismatches))


def validate_state_payload(payload: Dict[str, Any], contracts_dir: Optional[Path] = None) -> None:
    validate_payload(payload, "state_schema.json", contracts_dir=contracts_dir)