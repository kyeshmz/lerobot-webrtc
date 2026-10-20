"""SDK configuration containing credential environment names, never credentials."""

from dataclasses import dataclass

from lerobot.robots.config import RobotConfig

from lerobot_webrtc.native_options import RemoteRobotOptions


@RobotConfig.register_subclass("webrtc")
@dataclass(kw_only=True)
class WebRTCRobotConfig(RobotConfig, RemoteRobotOptions):
    def __post_init__(self):
        # Neither base validator cooperatively calls super().__post_init__().
        RobotConfig.__post_init__(self)
        RemoteRobotOptions.__post_init__(self)
