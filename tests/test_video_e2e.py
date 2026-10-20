"""Real WebRTC camera and browser hops with simultaneous simulated joint control."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from itertools import pairwise
from types import SimpleNamespace

import pytest
from aiohttp import ClientSession, encode_basic_auth
from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription

from lerobot_webrtc.arms import AsyncArm, SimulatedArm
from lerobot_webrtc.peer import Peer
from lerobot_webrtc.runtime import run_control
from lerobot_webrtc.video import VideoSettings, prefer_video_codec
from lerobot_webrtc.video_sources import SyntheticVideoTrack
from lerobot_webrtc.video_viewer import WebRTCViewer

from .conftest import TOKEN


async def wait_for(predicate, tasks, timeout=10):
    async with asyncio.timeout(timeout):
        while not predicate():
            for task in tasks:
                if task.done():
                    task.result()
                    pytest.fail("Endpoint ended before the expected video/control progress")
            await asyncio.sleep(0.01)


async def connect_browser(viewer, codec, frames, receivers):
    browser = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    transceiver = browser.addTransceiver("video", direction="recvonly")
    prefer_video_codec(transceiver, codec)

    @browser.on("track")
    def track_received(track):
        async def receive():
            while len(frames) < 12:
                frame = await track.recv()
                frames.append((frame.width, frame.height, frame.pts))

        receivers.append(asyncio.create_task(receive()))

    try:
        await browser.setLocalDescription(await browser.createOffer())
        async with ClientSession(
            headers={"Authorization": encode_basic_auth("viewer", TOKEN)}
        ) as client:
            async with client.post(
                viewer.url + "/offer",
                json={"sdp": browser.localDescription.sdp, "type": "offer"},
                headers={"Origin": viewer.url},
            ) as response:
                assert response.status == 200, await response.text()
                answer = await response.json()
        await browser.setRemoteDescription(RTCSessionDescription(**answer))
        return browser
    except BaseException:
        await browser.close()
        raise


@asynccontextmanager
async def session(
    signaling_server,
    codec="vp8",
    *,
    source_enabled=True,
    viewer_enabled=True,
    source_codec=None,
    width=320,
    height=240,
    fps=10,
    model="so101",
):
    url, _ = signaling_server
    settings = VideoSettings(width=width, height=height, fps=fps, codec=codec)
    viewer = WebRTCViewer(TOKEN, 0, settings, frame_timeout=max(1, 3 / fps))

    class CountedSource(SyntheticVideoTrack):
        starts = 0

        async def start(self):
            self.starts += 1
            await super().start()

    source = CountedSource(replace(settings, codec=source_codec) if source_codec else settings)
    leader = SimulatedArm(model=model)
    follower = SimulatedArm(model=model)
    leader_peer = Peer(
        "leader", url, "video-e2e", TOKEN, video_viewer=viewer if viewer_enabled else None
    )
    follower_peer = Peer(
        "follower", url, "video-e2e", TOKEN, video_source=source if source_enabled else None
    )
    tasks = [
        asyncio.create_task(
            run_control(AsyncArm(leader, "leader"), leader_peer, model, 30, 0.3, None)
        ),
        asyncio.create_task(
            run_control(AsyncArm(follower, "follower"), follower_peer, model, 30, 0.3, None)
        ),
    ]
    frames, receivers = [], []
    browser = None
    try:
        if viewer_enabled:
            await wait_for(lambda: viewer.started.is_set(), tasks)
            assert not leader.connected and not follower.connected
            browser = await connect_browser(viewer, codec, frames, receivers)
        yield SimpleNamespace(
            source=source,
            viewer=viewer,
            leader=leader,
            follower=follower,
            leader_peer=leader_peer,
            follower_peer=follower_peer,
            tasks=tasks,
            frames=frames,
            receivers=receivers,
            browser=browser,
        )
    finally:
        for task in tasks + receivers:
            task.cancel()
        await asyncio.gather(*tasks, *receivers, return_exceptions=True)
        if browser is not None:
            await browser.close()
        if not viewer_enabled:
            await viewer.close()
        if not source_enabled:
            await source.aclose()


@pytest.mark.parametrize(
    "codec,width,height,fps,model",
    [
        ("vp8", 320, 240, 10, "so100"),
        ("h264", 640, 480, 15, "so101"),
        ("vp8", 320, 240, 10, "b601-rs"),
    ],
)
async def test_native_browser_receives_configured_video_while_joints_move(
    signaling_server, codec, width, height, fps, model
):
    async with session(
        signaling_server, codec, width=width, height=height, fps=fps, model=model
    ) as active:
        await wait_for(
            lambda: len(active.frames) >= 5 and active.follower.applied >= 5,
            active.tasks + active.receivers,
        )
        assert active.source.starts == 1
        assert active.viewer.frames_received >= 5
        assert {(frame_width, frame_height) for frame_width, frame_height, _ in active.frames} == {
            (width, height)
        }
        timestamps = [pts for _, _, pts in active.frames]
        assert all(second > first for first, second in pairwise(timestamps))
        for peer in (active.leader_peer, active.follower_peer):
            assert not peer.channel.ordered and peer.channel.maxRetransmits == 0
            assert "m=audio" not in peer.pc.localDescription.sdp
        assert "a=recvonly" in active.leader_peer.pc.localDescription.sdp
        assert "a=sendonly" in active.follower_peer.pc.localDescription.sdp


async def test_low_frame_rate_video_is_independent_of_joint_command_rate(signaling_server):
    async with session(signaling_server, fps=1) as active:
        await wait_for(lambda: len(active.frames) >= 3, active.tasks)
        assert active.follower.applied >= 20
        assert not active.follower_peer.closed.is_set()


async def test_live_video_cannot_refresh_a_command_watchdog(signaling_server, monkeypatch):
    async with session(signaling_server) as active:
        await wait_for(
            lambda: len(active.frames) >= 3 and active.follower.applied >= 3, active.tasks
        )
        monkeypatch.setattr(active.leader_peer, "send", lambda _: False)
        results = await asyncio.wait_for(asyncio.gather(*active.tasks, return_exceptions=True), 5)
        assert isinstance(results[1], TimeoutError)
        assert active.follower.held and not active.follower.connected
        writes = active.follower.applied
        await asyncio.sleep(0.1)
        assert active.follower.applied == writes


async def test_stopped_video_terminates_control_instead_of_silently_going_blind(signaling_server):
    async with session(signaling_server) as active:
        await wait_for(
            lambda: len(active.frames) >= 3 and active.follower.applied >= 3, active.tasks
        )
        active.source.stop()
        results = await asyncio.wait_for(asyncio.gather(*active.tasks, return_exceptions=True), 6)
        assert all(isinstance(result, Exception) for result in results), results
        assert active.leader_peer.closed.is_set() and active.follower_peer.closed.is_set()
        assert active.follower.held and not active.follower.connected


async def test_arm_is_held_before_waiting_for_video_cleanup(signaling_server, monkeypatch):
    async with session(signaling_server) as active:
        await wait_for(
            lambda: len(active.frames) >= 3 and active.follower.applied >= 3, active.tasks
        )
        entered, release = asyncio.Event(), asyncio.Event()
        original_close = active.source.aclose

        async def slow_video_close():
            entered.set()
            await release.wait()
            await original_close()

        monkeypatch.setattr(active.source, "aclose", slow_video_close)
        for task in active.tasks:
            task.cancel()
        try:
            await asyncio.wait_for(entered.wait(), 3)
            assert active.follower.held and not active.follower.connected
        finally:
            release.set()
            await asyncio.wait_for(asyncio.gather(*active.tasks, return_exceptions=True), 3)


@pytest.mark.parametrize("source_enabled,viewer_enabled", [(True, False), (False, True)])
async def test_video_requires_both_endpoints_to_opt_in_before_capture_or_motion(
    signaling_server, source_enabled, viewer_enabled
):
    async with session(
        signaling_server, source_enabled=source_enabled, viewer_enabled=viewer_enabled
    ) as active:
        results = await asyncio.wait_for(asyncio.gather(*active.tasks, return_exceptions=True), 8)
        assert all(isinstance(result, Exception) for result in results), results
        assert active.source.starts == 0
        assert active.follower.applied == 0
        assert not active.leader.connected and not active.follower.connected


async def test_incompatible_codecs_fail_before_source_start_or_motion(signaling_server):
    async with session(signaling_server, codec="h264", source_codec="vp8") as active:
        results = await asyncio.wait_for(asyncio.gather(*active.tasks, return_exceptions=True), 8)
        assert all(isinstance(result, Exception) for result in results), results
        assert active.source.starts == 0
        assert active.follower.applied == 0
