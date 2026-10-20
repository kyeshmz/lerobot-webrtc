"""Smoke-test a built distribution in a clean environment, outside its checkout."""

import importlib.util
import subprocess
import sys
from importlib.metadata import distribution
from pathlib import Path

import lerobot_webrtc


def main():
    installed = distribution("lerobot-webrtc")
    location = Path(lerobot_webrtc.__file__).resolve()
    assert Path(sys.prefix).resolve() in location.parents, location
    assert lerobot_webrtc.__version__ == installed.version
    assert installed.metadata["Requires-Python"] == ">=3.12"
    assert set(installed.metadata.get_all("Provides-Extra")) == {"hardware", "b601", "dev"}
    assert not any("git+" in item for item in installed.requires)
    for optional in ("lerobot", "torch", "motorbridge"):
        assert importlib.util.find_spec(optional) is None, f"Base install pulled in {optional}"

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
