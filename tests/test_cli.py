
import json
import subprocess
import sys

import pytest


def run_cli(*arguments):
    return subprocess.run(
        [sys.executable, "-m", "cv_module", *arguments],
        capture_output=True,
        text=True,
        timeout=10,
    )


@pytest.mark.parametrize(
    "scenario",
    ["free", "rising", "congested", "jam"],
)
def test_synthetic_cli(scenario):
    process = subprocess.Popen(
        [
            sys.executable, "-m", "cv_module",
            "--mode", "synthetic",
            "--scenario", scenario,
            "--interval", "0.1",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    try:
        line = process.stdout.readline()
        assert line, "No JSON output received"

        payload = json.loads(line)

        assert payload["schema_version"] == "0.2.0"
        assert payload["source"] == "synthetic"
        assert payload["node_id"] == "node-a"
        assert len(payload["lanes"]) >= 1
    finally:
        process.terminate()
        process.communicate(timeout=5)


def test_invalid_interval_cli():
    result = run_cli(
        "--mode", "synthetic",
        "--interval", "-1",
    )

    assert result.returncode != 0
    assert "interval" in result.stderr.lower()


def test_missing_video_cli():
    result = run_cli("--mode", "replay")

    assert result.returncode != 0
    assert "--video" in result.stderr
