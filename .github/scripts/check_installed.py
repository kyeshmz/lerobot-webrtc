"""Smoke-test a built distribution in a clean environment, outside its checkout."""

import asyncio
import importlib.util
import subprocess
import sys
from importlib.metadata import distribution
from pathlib import Path

import lerobot_webrtc


async def check_synthetic_video():
    from lerobot_webrtc.video import VideoSettings
    from lerobot_webrtc.video_sources import SyntheticVideoTrack
    from lerobot_webrtc.video_viewer import WebRTCViewer

    viewer = WebRTCViewer("package-smoke-test-token", 0)
    source = SyntheticVideoTrack(VideoSettings(width=32, height=32, fps=30))
    try:
        await source.start()
        frame = await source.recv()
        assert (frame.width, frame.height) == (32, 32)
    finally:
        await source.aclose()
        await viewer.close()


def main():
    installed = distribution("lerobot-webrtc")
    location = Path(lerobot_webrtc.__file__).resolve()
    assert Path(sys.prefix).resolve() in location.parents, location
    assert lerobot_webrtc.__version__ == installed.version
    assert installed.metadata["Requires-Python"] == ">=3.12"
    assert set(installed.metadata.get_all("Provides-Extra")) == {"hardware", "b601", "dev"}
    assert not any("git+" in item for item in installed.requires)
    for optional in ("lerobot", "torch", "motorbridge", "numpy", "cv2", "PIL"):
        assert importlib.util.find_spec(optional) is None, f"Base install pulled in {optional}"

    asyncio.run(check_synthetic_video())

    for prefix in (
        [sys.executable, "-m", "lerobot_webrtc"],
        [str(Path(sys.executable).with_name("lerobot-webrtc"))],
    ):
        for command in ([], ["serve"], ["leader"], ["follower"], ["bench"], ["calibrate"]):
            subprocess.run(
                [*prefix, *command, "--help"],
                check=True,
                timeout=15,
                stdout=subprocess.DEVNULL,
            )
    print(f"Installed {installed.metadata['Name']} {installed.version}: metadata and CLI passed")


if __name__ == "__main__":
    main()
