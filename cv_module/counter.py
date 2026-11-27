
from dataclasses import dataclass


@dataclass
class CountingLine:
    line_id: str
    start: tuple
    end: tuple
    direction: str = "both"

    def __post_init__(self):
        if self.start == self.end:
            raise ValueError("Counting line cannot have zero length")

        if self.direction not in {
            "both",
            "positive",
            "negative"
        }:
            raise ValueError("Invalid counting direction")


class LineCounter:

    def __init__(self, lines, dead_zone=3.0):
        self.lines = lines
        self.dead_zone = dead_zone
        self.previous_positions = {}
        self.counted = set()

    def signed_distance(self, point, line):
        x, y = point
        x1, y1 = line.start
        x2, y2 = line.end

        dx = x2 - x1
        dy = y2 - y1

        length = (dx ** 2 + dy ** 2) ** 0.5

        return (
            dx * (y - y1) - dy * (x - x1)
        ) / length

    def crosses_segment(
        self,
        previous,
        current,
        previous_distance,
        current_distance,
        line
    ):
        ratio = previous_distance / (
            previous_distance - current_distance
        )

        crossing_x = previous[0] + ratio * (
            current[0] - previous[0]
        )

        crossing_y = previous[1] + ratio * (
            current[1] - previous[1]
        )

        x1, y1 = line.start
        x2, y2 = line.end

        dx = x2 - x1
        dy = y2 - y1

        projection = (
            (crossing_x - x1) * dx
            + (crossing_y - y1) * dy
        ) / (dx ** 2 + dy ** 2)

        return 0 <= projection <= 1

    def update(self, vehicles):
        events = []

        for vehicle in vehicles:
            current = vehicle.center

            for line in self.lines:
                key = (line.line_id, vehicle.track_id)

                if key in self.counted:
                    continue

                current_distance = self.signed_distance(
                    current, line
                )

                if abs(current_distance) <= self.dead_zone:
                    continue

                previous = self.previous_positions.get(key)

                self.previous_positions[key] = current

                if previous is None:
                    continue

                previous_distance = self.signed_distance(
                    previous, line
                )

                if previous_distance * current_distance >= 0:
                    continue

                if previous_distance < 0:
                    movement = "positive"
                else:
                    movement = "negative"

                if line.direction not in ("both", movement):
                    continue

                if not self.crosses_segment(
                    previous,
                    current,
                    previous_distance,
                    current_distance,
                    line
                ):
                    continue

                self.counted.add(key)
                self.previous_positions.pop(key, None)

                events.append({
                    "line_id": line.line_id,
                    "track_id": vehicle.track_id,
                    "vehicle_class": vehicle.vehicle_class,
                    "direction": movement
                })

        return events
