import sys
from cv_module.config import load_config, to_pixel_point
def replay_video(video_path, loop=False, target_fps=None):
    import cv2
    video = cv2.VideoCapture(video_path)

    if not video.isOpened():
        raise ValueError(f"Cannot open video: {video_path}")

    fps = video.get(cv2.CAP_PROP_FPS)
    total_frames = video.get(cv2.CAP_PROP_FRAME_COUNT)


    if fps <= 0:
        video.release()
        raise ValueError("Cannot determine video FPS")
    if target_fps is not None:
        if target_fps <= 0:
            video.release()
            raise ValueError("Target FPS must be greater than 0")

        playback_fps = target_fps
    else:
        playback_fps = fps

    duration = total_frames / fps

    print(f"Video FPS: {fps}", file=sys.stderr)
    print(f"Video duration: {duration:.2f} seconds", file=sys.stderr)

    frame_count = 0

    while True:
        success, frame = video.read()

        if not success:
            if loop:
                video.set(cv2.CAP_PROP_POS_FRAMES, 0)
                continue
            break

        frame_count += 1
        cv2.imshow("Traffic Replay", frame)
        if cv2.waitKey(max(1, round(1000 / playback_fps))) & 0xFF == ord("q"):
            break

    video.release()
    cv2.destroyAllWindows()
    print(f"Total frames read: {frame_count}", file=sys.stderr)


def analyze_video(
    video_path,
    interval=5.0,
    line_y=0.6,
    direction="both",
    device="cpu",
    image_size=960,
    show=False,
    config_path=None
):
    import cv2
    import time
    import json
    from math import isfinite

    from cv_module.detector import VehicleDetector
    from cv_module.counter import CountingLine, LineCounter
    from cv_module.traffic_metrics import (
        TrafficMetrics,
        calculate_occupancy, 
        create_roi_mask
    )

    if not isfinite(interval) or interval <= 0:
        raise ValueError("Invalid interval")

    if config_path is None and not 0 < line_y < 1:
        raise ValueError("line_y must be between 0 and 1")
    video = cv2.VideoCapture(video_path)

    if not video.isOpened():
        video.release()
        raise ValueError(f"Cannot open video: {video_path}")

    frame_count = 0
    total_crossings = 0
    interval_start = 0.0
    start_time = None

    try:
        fps = video.get(cv2.CAP_PROP_FPS)
        width = int(video.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(video.get(cv2.CAP_PROP_FRAME_HEIGHT))

        if not isfinite(fps) or fps <= 0:
            raise ValueError("Cannot determine video FPS")

        if width <= 1 or height <= 1:
            raise ValueError("Invalid video dimensions")

        if interval < 1 / fps:
            raise ValueError("Interval is shorter than one frame")

        detector = VehicleDetector(
            device=device,
            image_size=image_size
        )

        if config_path is not None:
            config = load_config(config_path)

            roi_polygon = [
                to_pixel_point(point, width, height)
                for point in config["roi_polygon"]
            ]

            line_config = config["counting_line"]

            line = CountingLine(
                line_id=line_config["line_id"],
                start=to_pixel_point(
                    line_config["start"], width, height
                ),
                end=to_pixel_point(
                    line_config["end"], width, height
                ),
                direction=line_config["direction"]
            )

        else:
            roi_polygon = [
                (0, 0),
                (width - 1, 0),
                (width - 1, height - 1),
                (0, height - 1)
            ]

            line_position = int(height * line_y)

            line = CountingLine(
                line_id="lane-1",
                start=(0, line_position),
                end=(width - 1, line_position),
                direction=direction
            )

        roi = create_roi_mask(width, height, roi_polygon)

        counter = LineCounter([line])
        metrics = TrafficMetrics(line.line_id)

        start_time = time.perf_counter()

        while True:
            success, frame = video.read()

            if not success:
                break

            frame_time = frame_count / fps

            while frame_time >= interval_start + interval:
                snapshot = metrics.snapshot(interval)

                print(
                    f"CV-2 metrics: {json.dumps(snapshot)}",
                    file=sys.stderr,
                    flush=True
                )

                metrics.reset()
                interval_start += interval

            vehicles = detector.detect_and_track(frame)
            events = counter.update(vehicles)

            for event in events:
                metrics.record_crossing(
                    event["vehicle_class"]
                )
                total_crossings += 1

            occupancy = calculate_occupancy(vehicles, roi)
            metrics.record_occupancy(occupancy)

            frame_count += 1

            if show:
                for vehicle in vehicles:
                    x1, y1, x2, y2 = map(int, vehicle.bbox)

                    cv2.rectangle(
                        frame,
                        (x1, y1),
                        (x2, y2),
                        (0, 255, 0),
                        2
                    )

                    label = (
                        f"{vehicle.vehicle_class} "
                        f"ID:{vehicle.track_id}"
                    )

                    cv2.putText(
                        frame,
                        label,
                        (x1, max(15, y1 - 10)),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        0.5,
                        (0, 255, 0),
                        2
                    )

                import numpy as np

                cv2.polylines(
                    frame,
                    [np.array(roi_polygon, dtype=np.int32)],
                    isClosed=True,
                    color=(255, 0, 0),
                    thickness=2
                )

                cv2.line(
                    frame,
                    line.start,
                    line.end,
                    (0, 255, 255),
                    2
                )

                cv2.putText(
                    frame,
                    f"Count: {total_crossings}",
                    (20, 35),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    1,
                    (0, 255, 255),
                    2
                )

                cv2.imshow("Traffic Analysis", frame)

                if cv2.waitKey(1) & 0xFF == ord("q"):
                    break

        video_elapsed = frame_count / fps
        remaining = video_elapsed - interval_start

        if remaining > 0 and metrics.frame_count > 0:
            snapshot = metrics.snapshot(remaining)

            print(
                f"CV-2 metrics: {json.dumps(snapshot)}",
                file=sys.stderr,
                flush=True
            )

    finally:
        video.release()

        if show:
            cv2.destroyAllWindows()

    elapsed = time.perf_counter() - start_time
    processing_fps = frame_count / elapsed if elapsed > 0 else 0

    print(
        f"Processed frames: {frame_count}",
        file=sys.stderr
    )

    print(
        f"Total vehicle crossings: {total_crossings}",
        file=sys.stderr
    )

    print(
        f"Video FPS: {fps:.2f}",
        file=sys.stderr
    )

    print(
        f"Processing FPS: {processing_fps:.2f}",
        file=sys.stderr
    )

    return {
        "processed_frames": frame_count,
        "total_crossings": total_crossings,
        "video_fps": fps,
        "processing_fps": processing_fps
    }

