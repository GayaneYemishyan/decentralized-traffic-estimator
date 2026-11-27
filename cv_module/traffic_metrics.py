
import numpy as np
from math import isfinite


def create_roi_mask(width, height, polygon):
    import cv2

    mask = np.zeros((height, width), dtype=np.uint8)

    points = np.array(polygon, dtype=np.int32)

    cv2.fillPoly(mask, [points], 1)

    if not np.any(mask):
        raise ValueError("Invalid ROI polygon")

    return mask


def calculate_occupancy(vehicles, roi):

    if isinstance(roi, np.ndarray):
        roi_mask = roi
        height, width = roi_mask.shape

        x1, y1 = 0, 0
        x2, y2 = width, height

    else:
        x1, y1, x2, y2 = roi

        width = x2 - x1
        height = y2 - y1

        if width <= 0 or height <= 0:
            raise ValueError("Invalid ROI")

        roi_mask = np.ones(
            (height, width),
            dtype=np.uint8
        )

    vehicle_mask = np.zeros(
        (height, width),
        dtype=np.uint8
    )

    for vehicle in vehicles:
        bx1, by1, bx2, by2 = vehicle.bbox

        left = max(x1, int(np.floor(bx1)))
        top = max(y1, int(np.floor(by1)))
        right = min(x2, int(np.ceil(bx2)))
        bottom = min(y2, int(np.ceil(by2)))

        if right <= left or bottom <= top:
            continue

        vehicle_mask[
            top - y1:bottom - y1,
            left - x1:right - x1
        ] = 1

    occupied_pixels = np.count_nonzero(
        vehicle_mask & roi_mask
    )

    roi_pixels = np.count_nonzero(roi_mask)

    if roi_pixels == 0:
        raise ValueError("ROI cannot be empty")

    return float(occupied_pixels / roi_pixels)


class TrafficMetrics:

    def __init__(self, line_id):
        self.line_id = line_id
        self.reset()

    def reset(self):
        self.counts_by_class = {
            "car": 0,
            "motorcycle": 0,
            "bus": 0,
            "truck": 0
        }

        self.occupancy_sum = 0.0
        self.frame_count = 0

    def record_crossing(self, vehicle_class):
        if vehicle_class not in self.counts_by_class:
            raise ValueError("Unknown vehicle class")

        self.counts_by_class[vehicle_class] += 1

    def record_occupancy(self, occupancy):
        if not 0 <= occupancy <= 1:
            raise ValueError(
                "Occupancy must be between 0 and 1"
            )

        self.occupancy_sum += occupancy
        self.frame_count += 1

    def snapshot(self, interval_seconds):
        if not isfinite(interval_seconds) or interval_seconds <= 0:
            raise ValueError(
                "Interval must be finite and positive"
            )

        vehicle_count = sum(
            self.counts_by_class.values()
        )

        flow_rate = (
            vehicle_count * 3600 / interval_seconds
        )

        avg_occupancy = (
            self.occupancy_sum / self.frame_count
            if self.frame_count > 0
            else 0.0
        )

        return {
            "lane_id": self.line_id,
            "vehicle_count": vehicle_count,
            "flow_rate_vph": round(flow_rate, 2),
            "avg_occupancy": round(avg_occupancy, 4),
            "counts_by_class": self.counts_by_class.copy()
        }
