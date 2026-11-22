import cv2
from unittest.mock import patch

import pytest

from cv_module.replay import replay_video


def test_invalid_video_path():
    with pytest.raises(ValueError, match="Cannot open video"):
        replay_video("nonexistent_video.mp4")


def test_video_replay():
    with patch.object(cv2, "imshow"), \
     patch.object(cv2, "waitKey", return_value=-1), \
     patch.object(cv2, "destroyAllWindows"):

        replay_video("videos/traffic.mp4", loop=False)


def test_invalid_target_fps():
    with pytest.raises(ValueError, match="Target FPS"):
        replay_video("videos/traffic.mp4", target_fps=-5)
