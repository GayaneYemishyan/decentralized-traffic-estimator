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

    print(f"Video FPS: {fps}")
    print(f"Video duration: {duration:.2f} seconds")

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
    print(f"Total frames read: {frame_count}")
