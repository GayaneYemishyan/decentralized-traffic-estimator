import numpy as np
import pytest

from cv_module.detector import Vehicle
from cv_module.traffic_metrics import (
    TrafficMetrics,
    calculate_occupancy,
    create_roi_mask
)


def test_vehicle_count():
    metrics = TrafficMetrics("lane-1")

    metrics.record_crossing("car")
    metrics.record_crossing("bus")

    result = metrics.snapshot(5)

    assert result["vehicle_count"] == 2
    assert result["counts_by_class"]["car"] == 1
    assert result["counts_by_class"]["bus"] == 1
    assert result["flow_rate_vph"] == 1440


def test_average_occupancy():
    metrics = TrafficMetrics("lane-1")

    metrics.record_occupancy(0.2)
    metrics.record_occupancy(0.4)

    result = metrics.snapshot(5)

    assert result["avg_occupancy"] == pytest.approx(0.3)


def test_empty_interval():
    metrics = TrafficMetrics("lane-1")

    result = metrics.snapshot(5)

    assert result["vehicle_count"] == 0
    assert result["flow_rate_vph"] == 0
    assert result["avg_occupancy"] == 0


def test_reset():
    metrics = TrafficMetrics("lane-1")

    metrics.record_crossing("truck")
    metrics.record_occupancy(0.5)

    metrics.reset()

    result = metrics.snapshot(5)

    assert result["vehicle_count"] == 0
    assert result["avg_occupancy"] == 0


def test_occupancy():
    vehicle = Vehicle(
        track_id=1,
        vehicle_class="car",
        bbox=(0, 0, 50, 50),
        confidence=0.9
    )

    occupancy = calculate_occupancy(
        [vehicle],
        (0, 0, 100, 100)
    )

    assert occupancy == pytest.approx(0.25)


def test_overlapping_boxes():
    vehicles = [
        Vehicle(1, "car", (0, 0, 50, 50), 0.9),
        Vehicle(2, "car", (0, 0, 50, 50), 0.9)
    ]

    occupancy = calculate_occupancy(
        vehicles,
        (0, 0, 100, 100)
    )

    assert occupancy == pytest.approx(0.25)


def test_invalid_interval():
    metrics = TrafficMetrics("lane-1")

    with pytest.raises(ValueError):
        metrics.snapshot(0)


def test_invalid_occupancy():
    metrics = TrafficMetrics("lane-1")

    with pytest.raises(ValueError):
        metrics.record_occupancy(1.5)

def test_create_roi_mask():
    polygon = [
        (2, 2),
        (7, 2),
        (7, 7),
        (2, 7)
    ]

    mask = create_roi_mask(10, 10, polygon)

    assert mask.shape == (10, 10)
    assert mask[4, 4] == 1
    assert mask[0, 0] == 0


def test_polygon_roi_empty_vehicles():
    roi = create_roi_mask(
        10, 10,
        [(2, 2), (7, 2), (7, 7), (2, 7)]
    )

    assert calculate_occupancy([], roi) == 0.0


def test_polygon_roi_vehicle_inside():
    from types import SimpleNamespace

    roi = create_roi_mask(
        10, 10,
        [(2, 2), (7, 2), (7, 7), (2, 7)]
    )

    vehicle = SimpleNamespace(bbox=(3, 3, 6, 6))

    result = calculate_occupancy([vehicle], roi)

    assert result == pytest.approx(9 / 36)


def test_polygon_roi_vehicle_outside():
    from types import SimpleNamespace

    roi = create_roi_mask(
        10, 10,
        [(2, 2), (7, 2), (7, 7), (2, 7)]
    )

    vehicle = SimpleNamespace(bbox=(0, 0, 2, 2))

    assert calculate_occupancy([vehicle], roi) == 0.0


def test_polygon_roi_partial_overlap():
    from types import SimpleNamespace

    roi = create_roi_mask(
        10, 10,
        [(2, 2), (7, 2), (7, 7), (2, 7)]
    )

    vehicle = SimpleNamespace(bbox=(0, 0, 4, 4))

    result = calculate_occupancy([vehicle], roi)

    assert result == pytest.approx(4 / 36)


def test_polygon_roi_overlapping_vehicles():
    from types import SimpleNamespace

    roi = create_roi_mask(
        10, 10,
        [(2, 2), (7, 2), (7, 7), (2, 7)]
    )

    vehicle1 = SimpleNamespace(bbox=(3, 3, 6, 6))
    vehicle2 = SimpleNamespace(bbox=(3, 3, 6, 6))

    result = calculate_occupancy(
        [vehicle1, vehicle2], roi
    )

    assert result == pytest.approx(9 / 36)