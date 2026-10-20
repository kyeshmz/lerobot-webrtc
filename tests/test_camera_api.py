"""Inspect both installed camera APIs without constructing or connecting a camera."""

import inspect

import pytest


def assert_camera_api():
    from lerobot.cameras.configs import ColorMode
    from lerobot.cameras.opencv import OpenCVCamera, OpenCVCameraConfig

    config = OpenCVCameraConfig(index_or_path=0, width=640, height=480, fps=15, fourcc="MJPG")
    assert config.color_mode == ColorMode.RGB
    assert config.fourcc == "MJPG"
    assert "warmup" in inspect.signature(OpenCVCamera.connect).parameters
    assert "timeout_ms" in inspect.signature(OpenCVCamera.async_read).parameters
    assert not inspect.iscoroutinefunction(OpenCVCamera.async_read)
    assert callable(OpenCVCamera.disconnect)


@pytest.mark.hardware_api
def test_released_lerobot_camera_api_without_camera_io():
    assert_camera_api()


@pytest.mark.b601_api
def test_source_snapshot_camera_api_without_camera_io():
    assert_camera_api()
