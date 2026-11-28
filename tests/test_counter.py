
from cv_module.detector import Vehicle
from cv_module.counter import CountingLine, LineCounter


def make_vehicle(track_id, y, x=50):
    return Vehicle(
        track_id=track_id,
        vehicle_class="car",
        bbox=(x - 5, y - 5, x + 5, y + 5),
        confidence=0.9
    )


def make_counter(direction="both"):
    line = CountingLine(
        line_id="lane-1",
        start=(0, 50),
        end=(100, 50),
        direction=direction
    )

    return LineCounter([line])


def test_line_crossing():
    counter = make_counter()

    assert counter.update([make_vehicle(1, 30)]) == []

    events = counter.update([make_vehicle(1, 70)])

    assert len(events) == 1
    assert events[0]["track_id"] == 1
    assert events[0]["vehicle_class"] == "car"


def test_no_double_counting():
    counter = make_counter()

    counter.update([make_vehicle(1, 30)])

    assert len(counter.update([make_vehicle(1, 70)])) == 1
    assert counter.update([make_vehicle(1, 30)]) == []
    assert counter.update([make_vehicle(1, 80)]) == []


def test_no_crossing():
    counter = make_counter()

    counter.update([make_vehicle(1, 10)])

    assert counter.update([make_vehicle(1, 30)]) == []


def test_direction_filter():
    counter = make_counter("positive")

    counter.update([make_vehicle(1, 70)])

    assert counter.update([make_vehicle(1, 30)]) == []


def test_positive_direction():
    counter = make_counter("positive")

    counter.update([make_vehicle(1, 30)])

    assert len(counter.update([make_vehicle(1, 70)])) == 1


def test_crossing_outside_line():
    counter = make_counter()

    counter.update([make_vehicle(1, 30, x=150)])

    assert counter.update(
        [make_vehicle(1, 70, x=150)]
    ) == []


def test_multiple_vehicles():
    counter = make_counter()

    counter.update([
        make_vehicle(1, 30),
        make_vehicle(2, 30)
    ])

    events = counter.update([
        make_vehicle(1, 70),
        make_vehicle(2, 70)
    ])

    assert len(events) == 2


def test_dead_zone():
    counter = make_counter()

    counter.update([make_vehicle(1, 30)])

    assert counter.update([make_vehicle(1, 49)]) == []
    assert counter.update([make_vehicle(1, 51)]) == []

    assert len(counter.update([make_vehicle(1, 70)])) == 1
