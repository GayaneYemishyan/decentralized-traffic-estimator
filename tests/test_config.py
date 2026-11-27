import json
import pytest

from cv_module.config import (
    validate_point,
    load_config,
    to_pixel_point
)


def test_valid_point():
    validate_point([0.5, 0.8])


def test_boundary_point():
    validate_point([0, 1])


def test_invalid_coordinate():
    with pytest.raises(ValueError):
        validate_point([1.5, 0.5])


def test_negative_coordinate():
    with pytest.raises(ValueError):
        validate_point([-0.1, 0.5])


def test_invalid_point_length():
    with pytest.raises(ValueError):
        validate_point([0.5])


def test_to_pixel_point():
    result = to_pixel_point([0.5, 0.5], 101, 201)

    assert result == (50, 100)


@pytest.fixture
def valid_config():
    return {
        "roi_polygon": [
            [0.2, 0.4],
            [0.8, 0.4],
            [1.0, 1.0],
            [0.0, 1.0]
        ],
        "counting_line": {
            "line_id": "lane-1",
            "start": [0.2, 0.6],
            "end": [0.8, 0.6],
            "direction": "both"
        }
    }


def test_load_valid_config(tmp_path, valid_config):
    path = tmp_path / "traffic.json"
    path.write_text(json.dumps(valid_config))

    result = load_config(path)

    assert result == valid_config


def test_invalid_polygon(tmp_path, valid_config):
    valid_config["roi_polygon"] = [
        [0.2, 0.4],
        [0.8, 0.4]
    ]

    path = tmp_path / "traffic.json"
    path.write_text(json.dumps(valid_config))

    with pytest.raises(ValueError):
        load_config(path)


def test_invalid_direction(tmp_path, valid_config):
    valid_config["counting_line"]["direction"] = "wrong"

    path = tmp_path / "traffic.json"
    path.write_text(json.dumps(valid_config))

    with pytest.raises(ValueError):
        load_config(path)


def test_zero_length_line(tmp_path, valid_config):
    valid_config["counting_line"]["end"] = (
        valid_config["counting_line"]["start"]
    )

    path = tmp_path / "traffic.json"
    path.write_text(json.dumps(valid_config))

    with pytest.raises(ValueError):
        load_config(path)