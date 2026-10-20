"""Configuration shared by the optional LeRobot plugin and its transport client."""

from __future__ import annotations

import argparse
import math
import re
from dataclasses import dataclass, field

from .models import arm_model
from .network import signaling_url, stun_url, turn_url
from .video import VideoSettings


@dataclass(kw_only=True)
class RemoteRobotOptions:
    model: str = "so101"
    signaling: str = "ws://127.0.0.1:8080"
    room: str = "native"
    token_env: str = "LEROBOT_WEBRTC_TOKEN"
    allow_physical: bool = False
    remote_id: str | None = None
    connect_timeout: float = 45
    request_timeout: float = 2
    observation_timeout: float = 0.5
    camera_name: str | None = None
    video_width: int = 640
    video_height: int = 480
    video_fps: int = 15
    video_codec: str = "auto"
    video_timeout: float = 3
    stun: list[str] = field(default_factory=list)
    turn: str | None = None
    turn_username_env: str = "LEROBOT_TURN_USERNAME"
    turn_password_env: str = "LEROBOT_TURN_PASSWORD"

    def __post_init__(self):
        arm_model(self.model)
        if not isinstance(self.signaling, str):
            raise ValueError("Invalid native Robot signaling URL")
        if not isinstance(self.room, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", self.room):
            raise ValueError(
                "Native Robot room must be 1–64 letters, digits, underscores or hyphens"
            )
        for name in (self.token_env, self.turn_username_env, self.turn_password_env):
            if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", name):
                raise ValueError("Credentials must reference valid environment variable names")
        if type(self.allow_physical) is not bool:
            raise ValueError("allow_physical must be an explicit boolean")
        if self.remote_id is not None and (
            not isinstance(self.remote_id, str) or not 1 <= len(self.remote_id) <= 128
        ):
            raise ValueError("remote_id must be a nonempty host identifier")
        for name, value, maximum in (
            ("connect_timeout", self.connect_timeout, 120),
            ("request_timeout", self.request_timeout, 5),
            ("observation_timeout", self.observation_timeout, 2),
            ("video_timeout", self.video_timeout, 10),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or (not math.isfinite(value) or not 0.05 <= value <= maximum)
            ):
                raise ValueError(f"{name} must be finite and between 0.05 and {maximum} seconds")
        if self.camera_name is not None and (
            not isinstance(self.camera_name, str)
            or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", self.camera_name)
        ):
            raise ValueError("camera_name must be a simple image feature name, e.g. front")
        if not isinstance(self.stun, list) or any(not isinstance(url, str) for url in self.stun):
            raise ValueError("stun must be a list of STUN URLs")
        if self.turn is not None and not isinstance(self.turn, str):
            raise ValueError("turn must be a TURN URL or None")
        try:
            signaling_url(self.signaling)
            for url in self.stun:
                stun_url(url)
            if self.turn is not None:
                turn_url(self.turn)
        except argparse.ArgumentTypeError as exc:
            raise ValueError(str(exc)) from exc
        self.video_settings()

    def video_settings(self) -> VideoSettings:
        return VideoSettings(
            width=self.video_width,
            height=self.video_height,
            fps=self.video_fps,
            codec=self.video_codec,
        )
