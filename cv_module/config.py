import json
from math import isfinite


def validate_point(point):
    if not isinstance(point, (list, tuple)) or len(point) != 2:
        raise ValueError("Point must contain two coordinates")

    for coordinate in point:
        if (
            type(coordinate) not in (int, float)
            or not isfinite(coordinate)
            or not 0 <= coordinate <= 1
        ):
            raise ValueError(
                "Coordinates must be between 0 and 1"
            )


def load_config(config_path):
    with open(config_path, "r", encoding="utf-8") as file:
        config = json.load(file)

    polygon = config["roi_polygon"]

    if len(polygon) < 3:
        raise ValueError("ROI must contain at least 3 points")

    for point in polygon:
        validate_point(point)

    line = config["counting_line"]

    validate_point(line["start"])
    validate_point(line["end"])

    if line["start"] == line["end"]:
        raise ValueError("Counting line cannot have zero length")

    if line["direction"] not in {
        "both", "positive", "negative"
    }:
        raise ValueError("Invalid counting direction")

    return config


def to_pixel_point(point, width, height):
    return (
        int(point[0] * (width - 1)),
        int(point[1] * (height - 1))
    )