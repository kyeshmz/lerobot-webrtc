"""Optional LeRobot Robot plugin; explicitly discover ``lerobot_robot_webrtc``."""

from .config_webrtc import WebRTCRobotConfig
from .webrtc import WebRTCRobot

__all__ = ["WebRTCRobot", "WebRTCRobotConfig"]
