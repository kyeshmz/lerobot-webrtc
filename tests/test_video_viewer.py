"""Native WebRTC relay tests using loopback signaling and synthetic frames only."""

import asyncio
import base64
import hashlib
import subprocess
import sys
from fractions import Fraction

import pytest
import pytest_asyncio
from aiohttp import ClientConnectionError, ClientSession, encode_basic_auth
from aiortc import (
    AudioStreamTrack,
    RTCConfiguration,
    RTCPeerConnection,
    RTCSessionDescription,
    VideoStreamTrack,
)
from aiortc.mediastreams import MediaStreamError
from aiortc.sdp import SessionDescription
from av import VideoFrame

from lerobot_webrtc import video_viewer
from lerobot_webrtc.peer import Peer
from lerobot_webrtc.video import MAX_VIDEO_HEIGHT, MAX_VIDEO_WIDTH, VideoSettings
from lerobot_webrtc.video_viewer import WebRTCViewer, _LatestVideoTrack

from .conftest import TOKEN


def frame(number=0, width=64, height=48):
    image = VideoFrame(width, height, "yuv420p")
    for index, plane in enumerate(image.planes):
        plane.update(bytes([40 + number % 100 if index == 0 else 128]) * plane.buffer_size)
    image.pts = number * 3000
    image.time_base = Fraction(1, 90000)
    return image


class SyntheticTrack(VideoStreamTrack):
    async def recv(self):
        pts, time_base = await self.next_timestamp()
        image = frame(pts // 3000)
        image.pts, image.time_base = pts, time_base
        return image


class QueuedTrack(VideoStreamTrack):
    def __init__(self):
        super().__init__()
        self.queue = asyncio.Queue()

    async def recv(self):
        item = await self.queue.get()
        if isinstance(item, Exception):
            raise item
        return item


async def wait_until(predicate, timeout=2):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.005)


@pytest_asyncio.fixture
async def viewer():
    instance = WebRTCViewer(TOKEN, port=0)
    await instance.start()
    try:
        yield instance
    finally:
        await instance.close()


@pytest_asyncio.fixture
async def http():
    async with ClientSession(
        headers={"Authorization": encode_basic_auth("viewer", TOKEN)}
    ) as client:
        yield client


async def offer(client, viewer, sdp):
    return await client.post(
        f"{viewer.url}/offer",
        headers={"Origin": viewer.url},
        json={"type": "offer", "sdp": sdp},
    )


async def connect_browser(client, viewer, pc):
    pc.addTransceiver("video", direction="recvonly")
    await pc.setLocalDescription(await pc.createOffer())
    async with await offer(client, viewer, pc.localDescription.sdp) as response:
        assert response.status == 200, await response.text()
        answer = await response.json()
    await pc.setRemoteDescription(RTCSessionDescription(**answer))
    await asyncio.wait_for(viewer.browser_ready.wait(), 3)
    return answer


@pytest_asyncio.fixture
async def browser_sdp():
    pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    pc.addTransceiver("video", direction="recvonly")
    try:
        yield (await pc.createOffer()).sdp
    finally:
        await pc.close()


def test_constructor_does_not_open_resources_or_schedule_work(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Constructor performed I/O or created a task")

    monkeypatch.setattr(video_viewer, "RTCPeerConnection", forbidden)
    monkeypatch.setattr(video_viewer.web, "AppRunner", forbidden)
    monkeypatch.setattr(asyncio, "create_task", forbidden)
    instance = WebRTCViewer(TOKEN)
    assert instance.url == "http://127.0.0.1:8081"
    assert not instance.started.is_set()
    assert not instance.browser_ready.is_set()
    assert not instance.ready.is_set()
    assert instance.frames_received == 0
    instance.on_error(RuntimeError("No-op callback"))


@pytest.mark.parametrize("codec", ["auto", "vp8", "h264"])
async def test_native_browser_connects_before_source_and_receives_real_video(http, codec):
    baseline = asyncio.all_tasks()
    instance = WebRTCViewer(TOKEN, port=0, settings=VideoSettings(codec=codec))
    errors = []
    instance.on_error = errors.append
    pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    source = SyntheticTrack()
    try:
        await instance.start()
        answer = await connect_browser(http, instance, pc)
        assert instance.browser_ready.is_set()
        assert not instance.ready.is_set()
        assert instance.frames_received == 0
        assert instance._source is None
        assert pc.connectionState == "connected"
        assert instance._pc.connectionState == "connected"
        assert instance._pc.sctp is None
        video = SessionDescription.parse(answer["sdp"]).media[0]
        assert video.direction == "sendonly"
        if codec != "auto":
            assert {item.mimeType.lower() for item in video.rtp.codecs} == {f"video/{codec}"}

        instance.attach(source)
        await asyncio.wait_for(instance.ready.wait(), 1)
        remote = pc.getReceivers()[0].track
        received = [await asyncio.wait_for(remote.recv(), 3) for _ in range(4)]
        assert all((item.width, item.height) == (64, 48) for item in received)
        timestamps = [item.pts * item.time_base for item in received]
        assert all(a < b for a, b in zip(timestamps, timestamps[1:], strict=False))
        assert 30 <= bytes(received[0].planes[0])[0] <= 70
        assert instance.frames_received >= len(received)
        assert not errors

        async with await offer(http, instance, pc.localDescription.sdp) as response:
            assert response.status == 409
    finally:
        await instance.close()
        await pc.close()
        source.stop()
    assert not errors
    assert instance._frame is None
    assert instance._source is None
    await wait_until(lambda: not (asyncio.all_tasks() - baseline))


async def test_relay_encoder_failure_latches_peer_closed_with_healthy_inbound_video(
    viewer, http, monkeypatch
):
    peer = Peer("leader", "ws://unused", "relay-failure", TOKEN, video_viewer=viewer)
    browser = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    source = SyntheticTrack()
    errors = []

    def fail(error):
        errors.append(error)
        peer.fail(error)

    viewer.on_error = fail
    try:
        await connect_browser(http, viewer, browser)
        viewer.attach(source)
        remote = browser.getReceivers()[0].track
        for _ in range(3):
            await asyncio.wait_for(remote.recv(), 3)
        assert not peer.closed.is_set()
        outgoing = viewer._outgoing
        sender = viewer._pc.getSenders()[0]

        def fail_encode(image, force_keyframe):
            raise RuntimeError("Synthetic browser relay encoder failure")

        # Exercise aiortc's actual encoder worker and RTP loop, which catches this
        # exception and stops its track without closing the healthy DTLS transport.
        monkeypatch.setattr(sender._RTCRtpSender__encoder, "encode", fail_encode)
        await asyncio.wait_for(peer.closed.wait(), 1)
        assert isinstance(peer.error, ConnectionError)
        assert str(peer.error) == "Browser video relay ended unexpectedly"
        assert errors == [peer.error]
        assert outgoing.readyState == "ended"
        assert viewer._frame is None
        assert viewer._pc.connectionState == browser.connectionState == "connected"
        assert source.readyState == "live"
        await asyncio.wait_for(viewer._receive_task, 1)
        assert isinstance(await asyncio.wait_for(source.recv(), 1), VideoFrame)
        outgoing.stop()
        outgoing.stop()
        assert errors == [peer.error]
    finally:
        await peer.close()
        await browser.close()
        source.stop()
    assert errors == [peer.error]


async def test_loopback_binding_authentication_headers_and_static_native_page(viewer):
    assert viewer.started.is_set()
    assert viewer.port != 0
    assert viewer._runner.addresses == [("127.0.0.1", viewer.port)]
    async with ClientSession() as client:
        for method, path in [("GET", "/"), ("POST", "/offer"), ("GET", "/missing")]:
            async with client.request(
                method, viewer.url + path, headers={"Origin": viewer.url}
            ) as response:
                assert response.status == 401
                assert response.headers["WWW-Authenticate"].startswith('Basic realm="viewer"')
                assert response.headers["Cache-Control"] == "no-store"
        for auth in [encode_basic_auth("wrong", TOKEN), encode_basic_auth("viewer", "wrong")]:
            async with client.get(viewer.url, headers={"Authorization": auth}) as response:
                assert response.status == 401
        async with client.get(
            viewer.url, headers={"Authorization": encode_basic_auth("viewer", TOKEN)}
        ) as response:
            assert response.status == 200
            html = await response.text()
            assert "new RTCPeerConnection" in html
            assert "new MediaStream()" in html
            assert "srcObject" in html
            assert "addTransceiver('video', {direction: 'recvonly'})" in html
            assert "iceGatheringState" in html
            assert "dead-man" in html
            assert TOKEN not in html
            for forbidden in ("<img", "getUserMedia", "MediaRecorder", "setInterval", "<canvas"):
                assert forbidden not in html
            script = html.split("<script>")[1].split("</script>")[0]
            digest = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
            assert f"'sha256-{digest}'" in response.headers["Content-Security-Policy"]
            assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
            assert response.headers["Referrer-Policy"] == "no-referrer"
            assert response.headers["X-Frame-Options"] == "DENY"
            assert response.headers["X-Content-Type-Options"] == "nosniff"
            assert "Access-Control-Allow-Origin" not in response.headers


@pytest.mark.parametrize(
    "headers",
    [
        {"Host": "rebind.example"},
        {"Host": "127.0.0.1:1"},
        {"Host": "localhost:8081"},
        {"Origin": "http://evil.example"},
        {"Origin": "null"},
        {"Sec-Fetch-Site": "cross-site"},
        {"Sec-Fetch-Site": "same-site"},
    ],
)
async def test_rebinding_and_cross_site_requests_are_forbidden(viewer, http, headers):
    async with http.get(viewer.url, headers=headers) as response:
        assert response.status == 403
        assert response.headers["Cache-Control"] == "no-store"


async def test_post_requires_exact_origin_and_does_not_accept_query_auth(viewer, http):
    async with http.post(f"{viewer.url}/offer", json={}) as response:
        assert response.status == 403
    async with http.get(f"{viewer.url}/?token={TOKEN}") as response:
        assert response.status == 400
        assert TOKEN not in await response.text()
    async with http.options(
        f"{viewer.url}/offer", headers={"Origin": "http://evil.example"}
    ) as response:
        assert response.status == 403
        assert "Access-Control-Allow-Origin" not in response.headers


@pytest.mark.parametrize(
    "mutation",
    [
        lambda sdp: sdp.replace("a=recvonly", "a=sendrecv"),
        lambda sdp: sdp.replace("a=recvonly", "a=sendonly"),
        lambda sdp: sdp.replace("a=recvonly", "a=inactive"),
        lambda sdp: sdp.replace("m=video 9", "m=video 0"),
        lambda sdp: sdp.replace("m=video", "m=audio"),
        lambda sdp: sdp + sdp[sdp.index("m=video") :],
        lambda sdp: sdp + "m=application 9 UDP/DTLS/SCTP webrtc-datachannel\r\n",
        lambda sdp: "v=0\r\n",
        lambda sdp: "not SDP",
    ],
)
async def test_sdp_policy_rejects_before_creating_a_peer(
    viewer, http, browser_sdp, mutation, monkeypatch
):
    def forbidden(*args, **kwargs):
        pytest.fail("Unvalidated SDP reached the peer connection")

    monkeypatch.setattr(video_viewer, "RTCPeerConnection", forbidden)
    async with await offer(http, viewer, mutation(browser_sdp)) as response:
        assert response.status == 400
    assert viewer._pc is None
    assert viewer._offer_task is None


@pytest.mark.parametrize(
    "payload",
    [None, [], {}, {"type": "answer", "sdp": "x"}, {"type": "offer", "sdp": 1}],
)
async def test_invalid_offer_metadata(viewer, http, payload):
    async with http.post(
        f"{viewer.url}/offer", headers={"Origin": viewer.url}, json=payload
    ) as response:
        assert response.status in {400, 415}
    assert viewer._pc is None


async def test_request_size_limit_and_errors_do_not_log_secrets(viewer, http, caplog):
    async with http.post(
        f"{viewer.url}/offer",
        headers={"Origin": viewer.url},
        json={"type": "offer", "sdp": TOKEN + "x" * 65536},
    ) as response:
        assert response.status == 413
    async with await offer(http, viewer, TOKEN) as response:
        assert response.status == 400
        assert TOKEN not in await response.text()
    assert TOKEN not in caplog.text
    assert viewer._pc is None


async def test_initial_offer_failure_closes_partial_peer_and_permits_retry(
    viewer, http, monkeypatch
):
    created = []

    def factory(*args, **kwargs):
        pc = RTCPeerConnection(*args, **kwargs)
        if not created:

            async def fail(description):
                raise ValueError("Sensitive invalid SDP")

            pc.setRemoteDescription = fail
        created.append(pc)
        return pc

    monkeypatch.setattr(video_viewer, "RTCPeerConnection", factory)
    browser = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    errors = []
    viewer.on_error = errors.append
    try:
        browser.addTransceiver("video", direction="recvonly")
        await browser.setLocalDescription(await browser.createOffer())
        async with await offer(http, viewer, browser.localDescription.sdp) as response:
            assert response.status == 400
            assert "Sensitive" not in await response.text()
        assert created[0].connectionState == "closed"
        assert viewer._pc is None
        async with await offer(http, viewer, browser.localDescription.sdp) as response:
            assert response.status == 200
            answer = await response.json()
        await browser.setRemoteDescription(RTCSessionDescription(**answer))
        await asyncio.wait_for(viewer.browser_ready.wait(), 3)
        assert not errors
    finally:
        await viewer.close()
        await browser.close()


async def test_setup_timeout_retires_unconnected_browser_without_latching_error(
    viewer, http, browser_sdp, monkeypatch
):
    monkeypatch.setattr(video_viewer, "SETUP_TIMEOUT", 0.1)
    errors = []
    viewer.on_error = errors.append
    async with await offer(http, viewer, browser_sdp) as response:
        assert response.status == 200
    pc = viewer._pc
    await asyncio.wait_for(viewer._setup_task, 1)
    assert pc.connectionState == "closed"
    assert viewer._pc is None
    assert not viewer.browser_ready.is_set()
    assert not errors
    monkeypatch.setattr(video_viewer, "SETUP_TIMEOUT", 3)
    browser = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    try:
        await connect_browser(http, viewer, browser)
    finally:
        await viewer.close()
        await browser.close()


async def test_pending_offer_reserves_only_slot_and_close_joins_setup(
    viewer, http, browser_sdp, monkeypatch
):
    entered = asyncio.Event()
    created = []

    def factory(*args, **kwargs):
        pc = RTCPeerConnection(*args, **kwargs)
        created.append(pc)

        async def stalled(description):
            entered.set()
            await asyncio.Event().wait()

        pc.setRemoteDescription = stalled
        return pc

    monkeypatch.setattr(video_viewer, "RTCPeerConnection", factory)
    pending = asyncio.create_task(offer(http, viewer, browser_sdp))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        async with await offer(http, viewer, browser_sdp) as response:
            assert response.status == 409
        await asyncio.wait_for(viewer.close(), 1)
        assert created[0].connectionState == "closed"
        assert viewer._offer_task is None
        assert viewer._frame is None
    finally:
        result = (await asyncio.gather(pending, return_exceptions=True))[0]
        if not isinstance(result, BaseException):
            result.close()


async def test_http_setup_deadline_cancels_partial_negotiation_and_allows_retry(
    viewer, http, browser_sdp, monkeypatch
):
    created = []

    def factory(*args, **kwargs):
        pc = RTCPeerConnection(*args, **kwargs)
        if not created:

            async def stalled(description):
                await asyncio.Event().wait()

            pc.setRemoteDescription = stalled
        created.append(pc)
        return pc

    monkeypatch.setattr(video_viewer, "RTCPeerConnection", factory)
    monkeypatch.setattr(video_viewer, "SETUP_TIMEOUT", 0.05)
    async with await offer(http, viewer, browser_sdp) as response:
        assert response.status == 408
    assert created[0].connectionState == "closed"
    assert viewer._pc is None
    assert viewer._offer_task is None
    monkeypatch.setattr(video_viewer, "SETUP_TIMEOUT", 3)
    browser = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    try:
        await connect_browser(http, viewer, browser)
    finally:
        await viewer.close()
        await browser.close()


async def test_receiver_drains_independently_and_retains_only_latest_frame(viewer):
    track = QueuedTrack()
    viewer.attach(track)
    outgoing = _LatestVideoTrack(viewer)
    waiting = asyncio.create_task(outgoing.recv())
    await asyncio.sleep(0)
    assert not waiting.done()
    images = [frame(number) for number in range(50)]
    for image in images:
        track.queue.put_nowait(image)
    await wait_until(lambda: viewer.frames_received == 50)
    assert viewer._frame is images[-1]
    assert await waiting is images[-1]
    assert track.queue.empty()
    waiting = asyncio.create_task(outgoing.recv())
    await asyncio.sleep(0)
    assert not waiting.done(), "Latest frame must not be sent repeatedly"
    track.queue.put_nowait(frame(50))
    assert (await asyncio.wait_for(waiting, 1)).pts == 50 * 3000
    outgoing.stop()
    await viewer.close()
    assert viewer._receive_task.done()
    assert viewer._frame is None


@pytest.mark.parametrize("failure", ["timeout", "end", "stopped", "decode", "width", "height"])
async def test_inbound_errors_report_once_and_intentional_close_is_silent(failure):
    instance = WebRTCViewer(TOKEN, port=0, frame_timeout=0.05)
    track = QueuedTrack()
    errors = []
    instance.on_error = errors.append
    instance.attach(track)
    if failure == "end":
        track.queue.put_nowait(MediaStreamError())
    elif failure == "stopped":
        track.stop()
    elif failure == "decode":
        track.queue.put_nowait(ValueError("synthetic decoder failure"))
    elif failure == "width":
        track.queue.put_nowait(frame(width=MAX_VIDEO_WIDTH + 2))
    elif failure == "height":
        track.queue.put_nowait(frame(height=MAX_VIDEO_HEIGHT + 2))
    try:
        await wait_until(lambda: bool(errors))
        expected = (
            TimeoutError
            if failure == "timeout"
            else (ConnectionError if failure in {"end", "stopped"} else ValueError)
        )
        assert isinstance(errors[0], expected)
        assert not instance.ready.is_set()
        assert instance.frames_received == 0
    finally:
        await instance.close()
        await instance.close()
    assert len(errors) == 1
    assert instance._frame is None
    assert instance._receive_task.done()


async def test_attach_rejects_audio_second_source_and_closed_viewer(viewer):
    source = QueuedTrack()
    audio = AudioStreamTrack()
    duplicate = QueuedTrack()
    try:
        with pytest.raises(ValueError, match="video only"):
            viewer.attach(audio)
        viewer.attach(source)
        with pytest.raises(ValueError, match="one source"):
            viewer.attach(duplicate)
        await viewer.close()
        with pytest.raises(RuntimeError, match="closed"):
            viewer.attach(duplicate)
    finally:
        audio.stop()
        source.stop()
        duplicate.stop()


@pytest.mark.parametrize("failure", ["failed", "disconnected", "closed"])
async def test_browser_failure_after_readiness_reports_error(viewer, http, failure):
    errors = []
    viewer.on_error = errors.append
    browser = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    try:
        await connect_browser(http, viewer, browser)
        pc = viewer._pc
        # aiortc does not expose browsers' disconnected state, so emit that transition.
        if failure == "closed":
            await browser.close()
        else:
            pc._RTCPeerConnection__connectionState = failure
            pc.emit("connectionstatechange")
        await wait_until(lambda: bool(errors))
        assert isinstance(errors[0], ConnectionError)
        assert not viewer.ready.is_set()
    finally:
        await viewer.close()
        await browser.close()
    assert len(errors) == 1


async def test_close_wakes_blocked_outgoing_and_joins_all_work_without_failure(viewer, http):
    baseline = asyncio.all_tasks()
    browser = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    source = QueuedTrack()
    errors = []
    viewer.on_error = errors.append
    try:
        await connect_browser(http, viewer, browser)
        viewer.attach(source)
        await asyncio.sleep(0)
        await asyncio.gather(viewer.close(), viewer.close())
        assert source.readyState == "ended"
        assert viewer._pc is None
        assert viewer._receive_task.done()
        assert viewer._setup_task.done()
        assert viewer._frame is None
        assert not viewer._cleanup_tasks
        assert not errors
        async with ClientSession() as client:
            with pytest.raises(ClientConnectionError):
                await client.get(viewer.url)
    finally:
        await viewer.close()
        await browser.close()
    await wait_until(lambda: not (asyncio.all_tasks() - baseline))


async def test_canceled_close_caller_does_not_cancel_owned_cleanup(viewer, http, monkeypatch):
    browser = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    entered, release = asyncio.Event(), asyncio.Event()
    try:
        await connect_browser(http, viewer, browser)
        pc = viewer._pc
        original_close = pc.close

        async def slow_close():
            entered.set()
            await release.wait()
            await original_close()

        monkeypatch.setattr(pc, "close", slow_close)
        caller = asyncio.create_task(viewer.close())
        await asyncio.wait_for(entered.wait(), 1)
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        assert not viewer._close_task.done()
        release.set()
        await asyncio.wait_for(viewer.close(), 1)
        assert pc.connectionState == "closed"
        assert not viewer._cleanup_tasks
    finally:
        release.set()
        await viewer.close()
        await browser.close()


async def test_close_before_start_is_idempotent_and_prevents_reuse():
    instance = WebRTCViewer(TOKEN, port=0)
    await asyncio.gather(instance.close(), instance.close())
    with pytest.raises(RuntimeError, match="closed"):
        await instance.start()
    assert instance._runner is None
    assert instance._frame is None


def test_import_does_not_require_optional_hardware_or_image_packages():
    program = """
import importlib.abc
import sys
class BlockOptional(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'cv2', 'torch', 'lerobot', 'motorbridge', 'PIL'}:
            raise AssertionError('Optional dependency imported: ' + fullname)
sys.meta_path.insert(0, BlockOptional())
from lerobot_webrtc.video_viewer import WebRTCViewer
WebRTCViewer('test-token')
"""
    result = subprocess.run(
        [sys.executable, "-c", program], capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stderr
