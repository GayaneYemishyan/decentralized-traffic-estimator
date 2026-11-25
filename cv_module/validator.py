from datetime import datetime
import json
from pathlib import Path

from jsonschema import validate


SCHEMA_PATH = (
    Path(__file__).resolve().parent.parent
    / "contracts"
    / "metrics_schema.json"
)


def validate_metrics(payload):
    with open(SCHEMA_PATH, "r") as file:
        schema = json.load(file)

    validate(instance=payload, schema=schema)
    for lane in payload["lanes"]:
        total = sum(lane["counts_by_class"].values())

        if lane["vehicle_count"] != total:
            raise ValueError(
                f"Vehicle count mismatch in {lane['lane_id']}"
            )
    start = datetime.fromisoformat(
        payload["interval_start"].replace("Z", "+00:00")
    )
    end = datetime.fromisoformat(
        payload["interval_end"].replace("Z", "+00:00")
    )

    if end <= start:
        raise ValueError(
            "interval_end must be later than interval_start"
        )

    lane_ids = [lane["lane_id"] for lane in payload["lanes"]]

    if len(lane_ids) != len(set(lane_ids)):
        raise ValueError("Duplicate lane_id found")
