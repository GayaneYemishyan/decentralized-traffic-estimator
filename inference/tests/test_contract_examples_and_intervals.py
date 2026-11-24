from __future__ import annotations

import json
from pathlib import Path

from inference.schema_validator import validate_metrics_payload, validate_state_payload
from inference.smoother import TemporalSmoother
from inference.state_builder import process_metrics


def test_process_metrics_supports_interval_start_end_and_schema_override() -> None:
    payload = {
        "schema_version": "0.2.0",
        "node_id": "node-1",
        "seq": 1,
        "interval_start": "2026-11-24T12:00:00.000Z",
        "interval_end": "2026-11-24T12:00:05.000Z",
        "source": "synthetic",
        "lanes": [
            {
                "lane_id": "lane-1",
                "vehicle_count": 2,
                "flow_rate_vph": 1440.0,
                "avg_occupancy": 0.25,
                "counts_by_class": {"car": 2},
            }
        ],
    }
    smoother = TemporalSmoother()
    state = process_metrics(payload, smoother=smoother, schema_version="0.2.0")

    assert state["schema_version"] == "0.2.0"
    assert state["node_id"] == "node-1"
    assert state["seq"] == 1
    assert state["timestamp"] == "2026-11-24T12:00:05.000Z"


def test_contract_examples_if_present() -> None:
    examples_dir = Path(__file__).resolve().parents[2] / "contracts" / "examples"
    if not examples_dir.exists():
        return

    example_files = sorted(examples_dir.glob("*.json"))
    assert example_files, "Expected example JSON files in contracts/examples"

    for example_path in example_files:
        data = json.loads(example_path.read_text(encoding="utf-8"))
        if "lanes" in data:
            validate_metrics_payload(data, contracts_dir=examples_dir.parent)
        else:
            validate_state_payload(data, contracts_dir=examples_dir.parent)
