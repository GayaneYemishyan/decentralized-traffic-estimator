
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest
from jsonschema import ValidationError

from cv_module.synthetic import generate_synthetic_metrics
from cv_module.validator import validate_metrics


@pytest.fixture
def valid_payload():
    start = datetime.now(timezone.utc)
    end = start + timedelta(seconds=2)

    def timestamp(value):
        return value.isoformat(timespec="milliseconds").replace(
            "+00:00", "Z"
        )

    return {
        "schema_version": "0.2.0",
        "node_id": "node-a",
        "seq": 0,
        "interval_start": timestamp(start),
        "interval_end": timestamp(end),
        "source": "synthetic",
        "lanes": [
            {
                "lane_id": "lane-1",
                **generate_synthetic_metrics("free", interval=2),
            }
        ],
    }


def test_valid_payload(valid_payload):
    validate_metrics(valid_payload)


def test_missing_required_field(valid_payload):
    payload = deepcopy(valid_payload)
    del payload["node_id"]

    with pytest.raises(ValidationError):
        validate_metrics(payload)


def test_invalid_occupancy(valid_payload):
    payload = deepcopy(valid_payload)
    payload["lanes"][0]["avg_occupancy"] = 1.5

    with pytest.raises(ValidationError):
        validate_metrics(payload)


def test_invalid_source(valid_payload):
    payload = deepcopy(valid_payload)
    payload["source"] = "unknown"

    with pytest.raises(ValidationError):
        validate_metrics(payload)


def test_vehicle_count_mismatch(valid_payload):
    payload = deepcopy(valid_payload)
    payload["lanes"][0]["vehicle_count"] += 1

    with pytest.raises(ValueError):
        validate_metrics(payload)


def test_invalid_interval_order(valid_payload):
    payload = deepcopy(valid_payload)
    payload["interval_end"] = payload["interval_start"]

    with pytest.raises(ValueError):
        validate_metrics(payload)


def test_duplicate_lane_ids(valid_payload):
    payload = deepcopy(valid_payload)
    payload["lanes"].append(deepcopy(payload["lanes"][0]))

    with pytest.raises(ValueError):
        validate_metrics(payload)

import json
from pathlib import Path

def test_shared_example_payload():
    example_path = (
        Path(__file__).resolve().parent.parent
        / "contracts"
        / "examples"
        / "metrics.example.json"
    )

    with open(example_path, "r") as file:
        payload = json.load(file)

    validate_metrics(payload)
