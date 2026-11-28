
from dataclasses import dataclass


VEHICLE_CLASSES = {
    2: "car",
    3: "motorcycle",
    5: "bus",
    7: "truck"
}


@dataclass
class Vehicle:
    track_id: int
    vehicle_class: str
    bbox: tuple
    confidence: float

    @property
    def center(self):
        x1, y1, x2, y2 = self.bbox
        return (
            (x1 + x2) / 2,
            (y1 + y2) / 2
        )


class VehicleDetector:

    def __init__(
        self,
        model_path="yolov8n.pt",
        confidence=0.1,
        device="cpu",
        image_size=640
    ):
        from ultralytics import YOLO

        self.model = YOLO(model_path)
        self.confidence = confidence
        self.device = device
        self.image_size = image_size

    def detect_and_track(self, frame):

        result = self.model.track(
            frame,
            persist=True,
            tracker="bytetrack.yaml",
            classes=list(VEHICLE_CLASSES.keys()),
            conf=self.confidence,
            device=self.device,
            imgsz=self.image_size,
            verbose=False
        )[0]

        vehicles = []
        boxes = result.boxes

        if boxes is None or boxes.id is None:
            return vehicles

        coordinates = boxes.xyxy.cpu().tolist()
        track_ids = boxes.id.int().cpu().tolist()
        class_ids = boxes.cls.int().cpu().tolist()
        confidences = boxes.conf.cpu().tolist()

        for bbox, track_id, class_id, confidence in zip(
            coordinates,
            track_ids,
            class_ids,
            confidences
        ):
            if class_id not in VEHICLE_CLASSES:
                continue

            vehicles.append(
                Vehicle(
                    track_id=track_id,
                    vehicle_class=VEHICLE_CLASSES[class_id],
                    bbox=tuple(bbox),
                    confidence=confidence
                )
            )

        return vehicles
