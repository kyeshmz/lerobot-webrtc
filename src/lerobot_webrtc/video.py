"""Shared limits and SDP policy for explicitly enabled, video-only media."""

from __future__ import annotations

import re
from dataclasses import dataclass

from aiortc import RTCRtpSender
from aiortc.sdp import SessionDescription

MAX_VIDEO_WIDTH = 1280
MAX_VIDEO_HEIGHT = 720
MAX_VIDEO_FPS = 30
VIDEO_CODECS = {"auto": None, "vp8": "video/VP8", "h264": "video/H264"}


@dataclass(frozen=True)
class VideoSettings:
    width: int = 640
    height: int = 480
    fps: int = 15
    codec: str = "auto"
    fourcc: str | None = None

    def __post_init__(self):
        if any(type(value) is not int for value in (self.width, self.height, self.fps)):
            raise ValueError("Video dimensions and fps must be integers")
        if not 16 <= self.width <= MAX_VIDEO_WIDTH or self.width % 2:
            raise ValueError(f"Video width must be even and between 16 and {MAX_VIDEO_WIDTH}")
        if not 16 <= self.height <= MAX_VIDEO_HEIGHT or self.height % 2:
            raise ValueError(f"Video height must be even and between 16 and {MAX_VIDEO_HEIGHT}")
        if not 1 <= self.fps <= MAX_VIDEO_FPS:
            raise ValueError(f"Video fps must be between 1 and {MAX_VIDEO_FPS}")
        if self.codec not in VIDEO_CODECS:
            raise ValueError("Video codec must be auto, vp8, or h264")
        if self.fourcc is not None and (
            not isinstance(self.fourcc, str) or not re.fullmatch(r"[A-Z0-9]{4}", self.fourcc)
        ):
            raise ValueError("Camera FOURCC must contain four uppercase letters or digits")


def prefer_video_codec(transceiver, codec: str) -> None:
    mime = VIDEO_CODECS[codec]
    if mime is not None:
        codecs = [
            item for item in RTCRtpSender.getCapabilities("video").codecs if item.mimeType == mime
        ]
        if not codecs:
            raise ValueError(f"This aiortc installation does not support {codec}")
        transceiver.setCodecPreferences(codecs)


def validate_video_sdp(sdp: str, direction: str | None, *, allow_application: bool = True) -> None:
    """Reject unrequested media before remote descriptions can create tracks."""
    if not isinstance(sdp, str) or not 0 < len(sdp) <= 65536:
        raise ValueError("Invalid media SDP")
    description = SessionDescription.parse(sdp)
    permitted = {"video", "application"} if allow_application else {"video"}
    if any(section.kind not in permitted for section in description.media):
        raise ValueError("Only explicitly requested video is supported; audio is disabled")
    videos = [section for section in description.media if section.kind == "video"]
    if direction is None:
        if videos:
            raise ValueError("Video requires explicit consent on both endpoints")
    elif len(videos) != 1 or videos[0].direction != direction or videos[0].port == 0:
        raise ValueError(f"Expected exactly one active {direction} video track")
