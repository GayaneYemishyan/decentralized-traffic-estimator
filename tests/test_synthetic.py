
import pytest

from cv_module.synthetic import generate_synthetic_metrics


SCENARIOS = ["free", "rising", "congested", "jam"]


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_synthetic_scenarios(scenario):
    metrics = generate_synthetic_metrics(scenario, interval=2)

    assert metrics["vehicle_count"] >= 0
    assert metrics["flow_rate_vph"] >= 0
    assert 0 <= metrics["avg_occupancy"] <= 1

    counts = metrics["counts_by_class"]

    assert set(counts) == {"car", "motorcycle", "bus", "truck"}
    assert all(count >= 0 for count in counts.values())
    assert metrics["vehicle_count"] == sum(counts.values())


@pytest.mark.parametrize(
    "scenario, minimum, maximum",
    [
        ("free", 0.05, 0.20),
        ("rising", 0.25, 0.70),
        ("congested", 0.60, 0.85),
        ("jam", 0.85, 0.98),
    ],
)
def test_occupancy_ranges(scenario, minimum, maximum):
    metrics = generate_synthetic_metrics(scenario, interval=2, step=5)

    assert minimum <= metrics["avg_occupancy"] <= maximum


def test_rising_traffic():
    first = generate_synthetic_metrics("rising", step=0)
    middle = generate_synthetic_metrics("rising", step=5)
    last = generate_synthetic_metrics("rising", step=10)

    assert (
        first["avg_occupancy"]
        < middle["avg_occupancy"]
        < last["avg_occupancy"]
    )


def test_invalid_scenario():
    with pytest.raises(ValueError):
        generate_synthetic_metrics("unknown")


def test_invalid_interval():
    with pytest.raises(ValueError):
        generate_synthetic_metrics("free", interval=-1)

def test_sudden_jam():
    before = generate_synthetic_metrics("jam", step=4)
    after = generate_synthetic_metrics("jam", step=5)

    assert 0.05 <= before["avg_occupancy"] <= 0.20
    assert 0.85 <= after["avg_occupancy"] <= 0.98
    assert after["avg_occupancy"] > before["avg_occupancy"]
