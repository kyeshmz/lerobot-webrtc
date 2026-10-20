import asyncio
from types import SimpleNamespace

import pytest
from aiortc import RTCPeerConnection

from lerobot_webrtc.peer import Peer
from lerobot_webrtc.video import VideoSettings, prefer_video_codec, validate_video_sdp


@pytest.mark.parametrize(
    "options",
    [
        {"width": 15},
        {"width": 641},
        {"width": 1282},
        {"height": 481},
        {"height": 722},
        {"fps": 0},
        {"fps": 31},
        {"fps": 15.0},
        {"width": True},
        {"codec": "av1"},
        {"fourcc": "mjpg"},
        {"fourcc": "MJPEG"},
        {"fourcc": "MJ G"},
    ],
)
def test_video_settings_reject_unsupported_or_unbounded_modes(options):
    with pytest.raises(ValueError):
        VideoSettings(**options)


def test_video_settings_keep_capture_format_separate_from_network_codec():
    settings = VideoSettings(width=1280, height=720, fps=30, codec="h264", fourcc="MJPG")
    assert settings.codec == "h264"
    assert settings.fourcc == "MJPG"


@pytest.mark.parametrize("codec", ["vp8", "h264"])
async def test_explicit_codec_preferences_limit_the_offer(codec):
    pc = RTCPeerConnection()
    try:
        transceiver = pc.addTransceiver("video", direction="recvonly")
        prefer_video_codec(transceiver, codec)
        offer = await pc.createOffer()
        assert f" {codec.upper()}/90000" in offer.sdp
        other = "H264" if codec == "vp8" else "VP8"
        assert f" {other}/90000" not in offer.sdp
    finally:
        await pc.close()


async def test_sdp_policy_requires_explicit_direction_and_disallows_extra_media():
    pc = RTCPeerConnection()
    try:
        pc.addTransceiver("video", direction="recvonly")
        offer = await pc.createOffer()
        validate_video_sdp(offer.sdp, "recvonly", allow_application=False)
        with pytest.raises(ValueError, match="consent"):
            validate_video_sdp(offer.sdp, None)
        with pytest.raises(ValueError, match="sendonly"):
            validate_video_sdp(offer.sdp, "sendonly")
        pc.addTransceiver("video", direction="recvonly")
        with pytest.raises(ValueError, match="exactly one"):
            validate_video_sdp((await pc.createOffer()).sdp, "recvonly")
        pc.addTransceiver("audio", direction="recvonly")
        with pytest.raises(ValueError, match="audio"):
            validate_video_sdp((await pc.createOffer()).sdp, "recvonly")
    finally:
        await pc.close()


async def test_browser_sdp_does_not_admit_a_data_channel():
    pc = RTCPeerConnection()
    try:
        pc.addTransceiver("video", direction="recvonly")
        pc.createDataChannel("unrequested")
        with pytest.raises(ValueError):
            validate_video_sdp((await pc.createOffer()).sdp, "recvonly", allow_application=False)
    finally:
        await pc.close()


async def test_peer_remains_data_only_by_default():
    peer = Peer("leader", "ws://localhost:1", "test", "secret")
    try:
        assert not peer.pc.getTransceivers()
    finally:
        await peer.close()


def test_peer_rejects_wrong_video_roles_before_allocating_a_connection():
    media = SimpleNamespace(settings=VideoSettings())
    with pytest.raises(ValueError, match="follower"):
        Peer("leader", "ws://localhost:1", "test", "secret", video_source=media)
    with pytest.raises(ValueError, match="leader"):
        Peer("follower", "ws://localhost:1", "test", "secret", video_viewer=media)


async def test_peer_waits_for_browser_before_signaling_and_frames_before_connecting():
    class Viewer:
        settings = VideoSettings()
        url = "http://127.0.0.1:8081"

        def __init__(self):
            self.started = asyncio.Event()
            self.browser_ready = asyncio.Event()
            self.ready = asyncio.Event()
            self.closed = False

        async def start(self):
            self.started.set()

        async def close(self):
            self.closed = True

    viewer = Viewer()
    peer = Peer("leader", "ws://localhost:1", "test", "secret", video_viewer=viewer)
    signaled = asyncio.Event()

    async def signal():
        signaled.set()
        peer.opened.set()
        await asyncio.Event().wait()

    peer.signal = signal
    task = asyncio.create_task(peer.connect(timeout=2))
    try:
        await viewer.started.wait()
        assert not signaled.is_set()
        viewer.browser_ready.set()
        await signaled.wait()
        assert not task.done()
        viewer.ready.set()
        await task
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await peer.close()
    assert viewer.closed
