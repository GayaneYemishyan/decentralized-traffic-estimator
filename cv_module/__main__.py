
import argparse
import json
import time
from datetime import datetime, timedelta, timezone
from cv_module.validator import validate_metrics
from cv_module.synthetic import generate_synthetic_metrics



def format_timestamp(dt):
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def main():
    parser = argparse.ArgumentParser(
        description="Traffic metrics generator"
    )

    parser.add_argument(
        "--scenario",
        choices=["free", "rising", "congested", "jam"],
        default="free"
    )

    parser.add_argument(
        "--interval",
        type=float,
        default=5.0
    )

    parser.add_argument(
        "--lanes",
        type=int,
        default=1
    )

    parser.add_argument(
        "--node-id",
        default="node-a"
    )

    parser.add_argument(
        "--mode",
        choices=["synthetic", "replay", "detect"],
        default="synthetic"
    )

    parser.add_argument("--video", type=str, default=None)
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--fps", type=float, default=None)

    parser.add_argument(
        "--line-y",
        type=float,
        default=0.6
    )

    parser.add_argument(
        "--direction",
        choices=["both", "positive", "negative"],
        default="both"
    )

    parser.add_argument(
        "--device",
        default="cpu"
    )

    parser.add_argument(
        "--imgsz",
        type=int,
        default=960
    )

    parser.add_argument(
        "--show",
        action="store_true"
    )

    parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="Path to traffic configuration JSON file"
    )

    args = parser.parse_args()
    if args.mode == "detect":
        if not args.video:
            parser.error("--video is required in detect mode")

        if args.loop or args.fps is not None:
            parser.error("--loop and --fps are for replay mode")

        if args.lanes != 1:
            parser.error("Detect mode currently uses one counting line")

        from cv_module.replay import analyze_video

        analyze_video(
            video_path=args.video,
            interval=args.interval,
            line_y=args.line_y,
            direction=args.direction,
            device=args.device,
            image_size=args.imgsz,
            show=args.show,
            config_path=args.config
        )
        return
    
    if args.mode == "replay":
        if not args.video:
            parser.error("--video is required in replay mode")

        from cv_module.replay import replay_video

        replay_video(args.video, loop=args.loop, target_fps=args.fps)
        return

    if args.interval <= 0:
        parser.error("--interval must be greater than 0")

    if not 1 <= args.lanes <= 16:
        parser.error("--lanes must be between 1 and 16")

    seq = 0
    interval_start = datetime.now(timezone.utc) - timedelta(
        seconds=args.interval
    )

    try:
        while True:
            interval_end = interval_start + timedelta(
            seconds=args.interval
            )

            lanes = []

            for lane_number in range(1, args.lanes + 1):
                metrics = generate_synthetic_metrics(
                    args.scenario,
                    args.interval,
                    step=seq
                )

                lanes.append({
                "lane_id": f"lane-{lane_number}",
                **metrics
                })

            payload = {
                "schema_version": "0.2.0",
                "node_id": args.node_id,
                "seq": seq,
                "interval_start": format_timestamp(interval_start),
                "interval_end": format_timestamp(interval_end),
                "source": "synthetic",
                "lanes": lanes
            }

            validate_metrics(payload)
            print(json.dumps(payload), flush=True)

            interval_start = interval_end
            seq += 1
            time.sleep(args.interval)


    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
