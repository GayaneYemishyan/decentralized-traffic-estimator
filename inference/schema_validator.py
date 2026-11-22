from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional


class SchemaValidationError(ValueError):
    """Raised when a metrics or state payload violates its contract schema."""


_ISO_MILLIS_UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def load_schema(schema_name: str, contracts_dir: Optional[Path] = None) -> Dict[str, Any]:
    base = contracts_dir if contracts_dir is not None else (_repo_root() / "contracts")
    schema_path = base / schema_name
    if not schema_path.exists():
        raise FileNotFoundError(f"Schema file not found: {schema_path}")
    with schema_path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


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
        return (isinstance(value, int) or isinstance(value, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    return True


def _validate_node(value: Any, schema: Dict[str, Any], path: str, errors: List[str]) -> None:
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
        if "pattern" in schema and not re.match(schema["pattern"], value):
            errors.append(f"{path}: value {value!r} does not match pattern {schema['pattern']!r}")
        if schema.get("format") == "date-time" and not _ISO_MILLIS_UTC_RE.match(value):
            errors.append(f"{path}: timestamp {value!r} does not match YYYY-MM-DDTHH:MM:SS.sssZ")

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: value {value} < minimum {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: value {value} > maximum {schema['maximum']}")

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
                _validate_node(item, item_schema, f"{path}[{idx}]", errors)

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
                _validate_node(value[key], prop_schema, f"{path}.{key}", errors)


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
        _validate_node(payload, schema, "$", errors)
        if errors:
            raise SchemaValidationError(f"Invalid {schema_name}: " + "; ".join(errors))
        return

    validator_cls = jsonschema.validators.validator_for(schema)
    validator = validator_cls(schema)
    validation_errors = sorted(validator.iter_errors(payload), key=lambda err: list(err.path))
    if validation_errors:
        formatted = "; ".join(
            f"{'.'.join(str(p) for p in err.path) or '$'}: {err.message}"
            for err in validation_errors
        )
        raise SchemaValidationError(f"Invalid {schema_name}: {formatted}")


def validate_metrics_payload(payload: Dict[str, Any], contracts_dir: Optional[Path] = None) -> None:
    validate_payload(payload, "metrics_schema.json", contracts_dir=contracts_dir)


def validate_state_payload(payload: Dict[str, Any], contracts_dir: Optional[Path] = None) -> None:
    validate_payload(payload, "state_schema.json", contracts_dir=contracts_dir)
