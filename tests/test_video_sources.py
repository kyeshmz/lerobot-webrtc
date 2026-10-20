import asyncio
import builtins
import subprocess
import sys
import threading
import time
from fractions import Fraction
from math import ceil
from types import ModuleType, SimpleNamespace

import pytest
from aiortc import VideoStreamTrack
from aiortc.mediastreams import MediaStreamError
from av import VideoFrame

from lerobot_webrtc import video_sources
from lerobot_webrtc.video import VideoSettings
from lerobot_webrtc.video_sources import LeRobotCameraTrack, SyntheticVideoTrack


async def wait_until(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0.001)


@pytest.fixture
def camera_sdk(monkeypatch):
    """No real SDK, OpenCV, numpy, image devices, or motors participate."""
    sdk = SimpleNamespace(
        calls=[], actions={}, cameras=[], images=[], converted=[], actual_fourcc=...
    )

    def operation(name, **kwargs):
        sdk.calls.append((name, threading.current_thread(), kwargs))
        action = sdk.actions.get(name)
        if isinstance(action, Exception):
            raise action
        if action is not None:
            action()

    class Config:
        def __init__(self, *, index_or_path, width, height, fps, fourcc, color_mode="RGB"):
            operation("config")
            self.index_or_path = index_or_path
            self.width = width
            self.height = height
            self.fps = fps
            self.fourcc = fourcc
            self.color_mode = color_mode

    class Camera:
        def __init__(self, config):
            operation("construct")
            self.config = config
            self.connected = False
            self.disconnects = 0
            self.videocapture = SimpleNamespace(get=self.get_capture_property)
            sdk.cameras.append(self)

        def get_capture_property(self, property_id):
            operation("fourcc", property_id=property_id)
            assert property_id == sys.modules["cv2"].CAP_PROP_FOURCC
            actual = self.config.fourcc if sdk.actual_fourcc is ... else sdk.actual_fourcc
            if isinstance(actual, str):
                return float(sum(ord(char) << (8 * index) for index, char in enumerate(actual)))
            return actual

        def connect(self, *, warmup):
            operation("connect", warmup=warmup)
            self.connected = True

        def async_read(self, *, timeout_ms):
            operation("read", timeout_ms=timeout_ms)
            if sdk.images:
                return sdk.images.pop(0)
            return SimpleNamespace(shape=(self.config.height, self.config.width, 3), dtype="uint8")

        def disconnect(self):
            self.disconnects += 1
            assert self.disconnects == 1, "SDK disconnect must not be repeated"
            operation("disconnect")
            self.connected = False

    def convert(image, *, format):
        operation("convert", format=format)
        sdk.converted.append(image)
        return VideoFrame(image.shape[1], image.shape[0], format=format)

    for name in ("lerobot", "lerobot.cameras", "lerobot.cameras.opencv"):
        monkeypatch.setitem(sys.modules, name, ModuleType(name))
    module = sys.modules["lerobot.cameras.opencv"]
    module.OpenCVCamera = Camera
    module.OpenCVCameraConfig = Config
    cv2 = ModuleType("cv2")
    cv2.CAP_PROP_FOURCC = 6
    monkeypatch.setitem(sys.modules, "cv2", cv2)
    original_import = builtins.__import__

    def import_on_worker(name, *args, **kwargs):
        if name == "lerobot.cameras.opencv":
            operation("import")
        elif name == "cv2":
            operation("import_cv2")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_on_worker)
    monkeypatch.setattr(video_sources, "VideoFrame", SimpleNamespace(from_ndarray=convert))
    return sdk


def assert_worker_released(sdk):
    threads = {thread for _, thread, _ in sdk.calls}
    assert all(not thread.is_alive() for thread in threads)


def install_camera_cadence(sdk, fps, now, wait):
    """One latest frame per capture interval, with realistic first-frame delay."""
    cadence = SimpleNamespace(
        initial_delay=max(0.35, 1 / fps + 0.05),
        interval=1 / fps + 0.02,
        next_frame=None,
        arrivals=[],
        timeouts=[],
    )

    def connect():
        cadence.next_frame = now() + cadence.initial_delay

    def read():
        timeout_ms = sdk.calls[-1][2]["timeout_ms"]
        cadence.timeouts.append(timeout_ms)
        delay = max(0, cadence.next_frame - now())
        wait(min(delay, timeout_ms / 1000))
        if now() + 1e-9 < cadence.next_frame:
            raise TimeoutError("No camera frame within the bounded read deadline")
        cadence.arrivals.append(now())
        cadence.next_frame += cadence.interval
        if cadence.next_frame <= now():
            # A delayed consumer receives the latest capture, not a frame queue.
            skipped = int((now() - cadence.next_frame) / cadence.interval) + 1
            cadence.next_frame += skipped * cadence.interval

    sdk.actions.update(connect=connect, read=read)
    return cadence


@pytest.fixture
def camera_clock(monkeypatch):
    clock = SimpleNamespace(now=100.0)

    def advance(seconds):
        clock.now += seconds

    clock.advance = advance
    # Do not replace the process clock used by asyncio's own deadlines.
    monkeypatch.setattr(video_sources, "time", SimpleNamespace(monotonic=lambda: clock.now))
    return clock


def test_constructors_have_no_io_tasks_or_workers(monkeypatch):
    def forbidden_executor(*args, **kwargs):
        pytest.fail("Constructors must not allocate an executor")

    monkeypatch.setattr(video_sources, "ThreadPoolExecutor", forbidden_executor)
    settings = VideoSettings()
    synthetic = SyntheticVideoTrack(settings)
    camera = LeRobotCameraTrack(7, settings)
    for track in (synthetic, camera):
        assert isinstance(track, VideoStreamTrack)
        assert track.settings is settings
        assert track.readyState == "live"
        track.on_error(RuntimeError("default callback is a no-op"))
        track.stop()
        assert track.readyState == "ended"


def test_synthetic_base_install_needs_no_optional_image_or_hardware_imports():
    script = """
import builtins
original_import = builtins.__import__
def guarded_import(name, *args, **kwargs):
    if name.split('.')[0] in {'numpy', 'PIL', 'cv2', 'lerobot'}:
        raise AssertionError('Optional dependency imported: ' + name)
    return original_import(name, *args, **kwargs)
builtins.__import__ = guarded_import
import asyncio
from lerobot_webrtc.video import VideoSettings
from lerobot_webrtc.video_sources import SyntheticVideoTrack, LeRobotCameraTrack
async def main():
    camera = LeRobotCameraTrack(0, VideoSettings())
    await camera.aclose()
    track = SyntheticVideoTrack(VideoSettings(width=18, height=22, fps=30))
    await track.start()
    first, second = await track.recv(), await track.recv()
    assert (first.width, first.height) == (18, 22)
    assert second.pts > first.pts
    await track.aclose()
asyncio.run(main())
print('base-only-ok')
"""
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "base-only-ok"


@pytest.mark.parametrize("width,height", [(18, 22), (640, 480), (1280, 720)])
async def test_synthetic_dimensions_changing_pattern_and_timestamps(width, height):
    settings = VideoSettings(width=width, height=height, fps=30)
    track = SyntheticVideoTrack(settings)
    try:
        await track.start()
        await track.start()
        frames = [await track.recv() for _ in range(3)]
        assert all((frame.width, frame.height) == (width, height) for frame in frames)
        assert all(frame.format.name == "yuv420p" for frame in frames)
        assert all(frame.time_base == Fraction(1, 90000) for frame in frames)
        assert frames[0].pts == 0
        assert frames[0].pts < frames[1].pts < frames[2].pts
        assert bytes(frames[0].planes[0]) != bytes(frames[1].planes[0])
        assert bytes(frames[1].planes[1]) != bytes(frames[2].planes[1])
    finally:
        await track.aclose()
        await track.aclose()
    with pytest.raises(MediaStreamError):
        await track.recv()
    with pytest.raises(MediaStreamError):
        await track.start()


async def test_synthetic_paces_without_catchup_after_a_stall():
    track = SyntheticVideoTrack(VideoSettings(width=16, height=16, fps=20))
    try:
        await track.start()
        first = await track.recv()
        started = time.monotonic()
        second = await track.recv()
        assert time.monotonic() - started >= 0.04
        await asyncio.sleep(0.18)
        late = await track.recv()
        started = time.monotonic()
        following = await track.recv()
        assert time.monotonic() - started >= 0.04
        assert late.pts - second.pts >= 14000
        assert following.pts > late.pts > second.pts > first.pts
    finally:
        await track.aclose()


async def test_synthetic_stop_wakes_slow_cadence_without_background_work():
    track = SyntheticVideoTrack(VideoSettings(width=16, height=16, fps=1))
    await track.start()
    await track.recv()
    receive = asyncio.create_task(track.recv())
    await asyncio.sleep(0)
    async with asyncio.timeout(0.2):
        await track.aclose()
        with pytest.raises(MediaStreamError):
            await receive
    await track.aclose()


async def test_camera_config_rgb_latest_frames_and_one_worker(camera_sdk, monkeypatch):
    settings = VideoSettings(width=18, height=22, fps=20, codec="vp8", fourcc="YUYV")
    track = LeRobotCameraTrack(7, settings)
    loop = asyncio.get_running_loop()
    original_run = loop.run_in_executor
    executors = []

    def owned_executor_only(executor, operation, *args):
        assert executor is not None, "Video must never use the default/arm executor"
        executors.append(executor)
        return original_run(executor, operation, *args)

    monkeypatch.setattr(loop, "run_in_executor", owned_executor_only)
    prime = SimpleNamespace(shape=(22, 18, 3), dtype="uint8")
    fresh = SimpleNamespace(shape=(22, 18, 3), dtype="uint8")
    camera_sdk.images.extend([prime, fresh])
    try:
        await track.start()
        assert [name for name, _, _ in camera_sdk.calls] == [
            "import",
            "config",
            "construct",
            "connect",
            "import_cv2",
            "fourcc",
            "read",
            "convert",
        ]
        await track.start()
        config = camera_sdk.cameras[0].config
        assert vars(config) == {
            "index_or_path": 7,
            "width": 18,
            "height": 22,
            "fps": 20,
            "fourcc": "YUYV",
            "color_mode": "RGB",
        }
        first = await track.recv()
        assert camera_sdk.converted[0] is prime
        assert camera_sdk.converted[1] is fresh  # Never deliver a stale startup frame.
        started = time.monotonic()
        second = await track.recv()
        assert time.monotonic() - started >= 0.04
        assert (first.width, first.height, first.format.name) == (18, 22, "rgb24")
        assert first.pts == 0 < second.pts
        assert first.time_base == second.time_base == Fraction(1, 90000)
        assert [kwargs["timeout_ms"] for name, _, kwargs in camera_sdk.calls if name == "read"] == [
            1000,
            250,
            250,
        ]
        assert all(
            kwargs == {"format": "rgb24"}
            for name, _, kwargs in camera_sdk.calls
            if name == "convert"
        )
        assert next(kwargs for name, _, kwargs in camera_sdk.calls if name == "connect") == {
            "warmup": False
        }
    finally:
        await track.aclose()
        await track.aclose()
    threads = {thread for _, thread, _ in camera_sdk.calls}
    assert len(threads) == 1
    assert threading.current_thread() not in threads
    assert len(set(executors)) == 1
    assert next(iter(set(executors)))._max_workers == 1
    assert camera_sdk.cameras[0].disconnects == 1
    assert_worker_released(camera_sdk)


@pytest.mark.parametrize("fps", range(1, 31))
async def test_camera_first_frame_and_recvs_allow_configured_cadence(camera_sdk, camera_clock, fps):
    cadence = install_camera_cadence(
        camera_sdk, fps, lambda: camera_clock.now, camera_clock.advance
    )
    track = LeRobotCameraTrack(0, VideoSettings(width=16, height=16, fps=fps))
    errors = []
    track.on_error = errors.append
    try:
        await track.start()
        assert cadence.arrivals[0] - 100 == pytest.approx(cadence.initial_delay)
        assert cadence.initial_delay > 0.2
        frames = [await track.recv()]
        assert len(cadence.arrivals) == 2  # Immediate recv must allow another full period.
        for _ in range(3):
            camera_clock.advance(1 / fps)
            frames.append(await track.recv())
        assert len(cadence.arrivals) == 5
        assert len(camera_sdk.converted) == 5
        assert all(
            later - earlier == pytest.approx(cadence.interval)
            for earlier, later in zip(cadence.arrivals, cadence.arrivals[1:], strict=False)
        )
        assert frames[0].pts == 0
        assert all(
            later.pts - earlier.pts == pytest.approx(cadence.interval * 90000, abs=1)
            for earlier, later in zip(frames, frames[1:], strict=False)
        )
        timeout_ms = ceil(1000 / fps) + 200
        assert cadence.timeouts == [max(1000, timeout_ms)] + [timeout_ms] * 4
        assert all(0 < timeout <= 1200 for timeout in cadence.timeouts)
        assert errors == []
        assert track.readyState == "live"
    finally:
        await track.aclose()
    assert_worker_released(camera_sdk)


async def test_one_fps_camera_with_real_capture_waits(camera_sdk):
    cadence = install_camera_cadence(camera_sdk, 1, time.monotonic, time.sleep)
    track = LeRobotCameraTrack(0, VideoSettings(width=16, height=16, fps=1))
    errors = []
    track.on_error = errors.append
    started = time.monotonic()
    try:
        async with asyncio.timeout(6):
            await track.start()
            first = await track.recv()
            second = await track.recv()
        assert cadence.arrivals[0] - started >= 1
        assert cadence.arrivals[1] - cadence.arrivals[0] >= 0.95
        assert second.pts - first.pts >= 85000
        assert cadence.timeouts == [1200, 1200, 1200]
        assert errors == []
    finally:
        await track.aclose()
    assert_worker_released(camera_sdk)


@pytest.mark.parametrize("fps", [1, 30])
@pytest.mark.parametrize("startup", [False, True])
async def test_camera_cadence_deadline_remains_bounded(camera_sdk, camera_clock, fps, startup):
    cadence = install_camera_cadence(
        camera_sdk, fps, lambda: camera_clock.now, camera_clock.advance
    )
    track = LeRobotCameraTrack(0, VideoSettings(width=16, height=16, fps=fps))
    errors = []
    track.on_error = errors.append
    try:
        if startup:
            cadence.initial_delay = 10
        else:
            await track.start()
            cadence.next_frame = camera_clock.now + 10
        started = camera_clock.now
        with pytest.raises(TimeoutError, match="bounded read deadline") as raised:
            await (track.start() if startup else track.recv())
        timeout_ms = ceil(1000 / fps) + 200
        if startup:
            timeout_ms = max(1000, timeout_ms)
        assert camera_clock.now - started == pytest.approx(timeout_ms / 1000)
        assert len(cadence.timeouts) == (1 if startup else 2)  # No retry/busy loop.
        assert errors == [raised.value]
        assert track.readyState == "ended"
    finally:
        await track.aclose()
    assert camera_sdk.cameras[0].disconnects == 1
    assert_worker_released(camera_sdk)


async def test_camera_requested_mjpg_is_verified_before_priming(camera_sdk):
    camera_sdk.actual_fourcc = "MJPG"
    track = LeRobotCameraTrack(7, VideoSettings(width=18, height=22, fps=3, fourcc="MJPG"))
    try:
        await track.start()
        names = [name for name, _, _ in camera_sdk.calls]
        assert names.index("connect") < names.index("fourcc") < names.index("read")
        config = camera_sdk.cameras[0].config
        assert (config.index_or_path, config.width, config.height, config.fps, config.fourcc) == (
            7,
            18,
            22,
            3,
            "MJPG",
        )
        assert track.readyState == "live"
    finally:
        await track.aclose()
    assert {thread for _, thread, _ in camera_sdk.calls} != {threading.current_thread()}
    assert len({thread for _, thread, _ in camera_sdk.calls}) == 1
    assert_worker_released(camera_sdk)


async def test_camera_ignored_fourcc_fails_before_priming(camera_sdk):
    camera_sdk.actual_fourcc = "YUYV"
    track = LeRobotCameraTrack(0, VideoSettings(width=16, height=16, fourcc="MJPG"))
    errors = []
    track.on_error = errors.append
    try:
        with pytest.raises(ValueError, match="FOURCC.*YUYV.*MJPG") as raised:
            await track.start()
        assert errors == [raised.value]
        assert track.readyState == "ended"
        assert not any(name == "read" for name, _, _ in camera_sdk.calls)
    finally:
        await track.aclose()
        await track.aclose()
    assert camera_sdk.cameras[0].disconnects == 1
    assert_worker_released(camera_sdk)


@pytest.mark.parametrize("unavailable", ["capture", "get", "cv2", None, float("nan"), 17.5])
async def test_camera_unavailable_fourcc_fails_closed(camera_sdk, unavailable):
    if unavailable == "capture":
        camera_sdk.actions["connect"] = lambda: delattr(camera_sdk.cameras[0], "videocapture")
    elif unavailable == "get":
        camera_sdk.actions["fourcc"] = RuntimeError("Backend cannot read FOURCC")
    elif unavailable == "cv2":
        camera_sdk.actions["import_cv2"] = ModuleNotFoundError("cv2 unavailable")
    else:
        camera_sdk.actual_fourcc = unavailable
    track = LeRobotCameraTrack(0, VideoSettings(width=16, height=16, fourcc="MJPG"))
    errors = []
    track.on_error = errors.append
    try:
        with pytest.raises(ValueError, match="Cannot verify requested camera FOURCC") as raised:
            await track.start()
        assert errors == [raised.value]
        assert not any(name == "read" for name, _, _ in camera_sdk.calls)
    finally:
        await track.aclose()
    assert camera_sdk.cameras[0].disconnects == 1
    assert_worker_released(camera_sdk)


async def test_camera_default_format_does_not_import_or_probe_cv2(camera_sdk):
    track = LeRobotCameraTrack(0, VideoSettings(width=16, height=16))
    try:
        await track.start()
        assert not any(name in {"import_cv2", "fourcc"} for name, _, _ in camera_sdk.calls)
    finally:
        await track.aclose()
    assert_worker_released(camera_sdk)


@pytest.mark.parametrize("stage", ["connect", "fourcc", "read", "convert", "disconnect"])
async def test_camera_blocking_work_does_not_block_event_loop(camera_sdk, stage):
    entered, release = threading.Event(), threading.Event()

    def block():
        entered.set()
        assert release.wait(2), "Event loop could not release the camera worker"

    track = LeRobotCameraTrack(
        0, VideoSettings(width=16, height=16, fourcc="MJPG" if stage == "fourcc" else None)
    )
    if stage == "disconnect":
        await track.start()
    camera_sdk.actions[stage] = block
    operation = asyncio.create_task(track.aclose() if stage == "disconnect" else track.start())
    try:
        await wait_until(entered.is_set)
        assert not operation.done()
    finally:
        release.set()
        await operation
        await track.aclose()
    assert_worker_released(camera_sdk)


@pytest.mark.parametrize("stage", ["import", "config", "construct", "connect", "read", "convert"])
async def test_partial_camera_setup_reports_failure_and_disconnects_once(camera_sdk, stage):
    error = RuntimeError(f"failed during {stage}")
    camera_sdk.actions[stage] = error
    track = LeRobotCameraTrack(0, VideoSettings(width=16, height=16))
    errors = []
    track.on_error = lambda exc: errors.append((exc, threading.current_thread()))
    with pytest.raises(RuntimeError) as raised:
        await track.start()
    assert raised.value is error
    assert errors == [(error, threading.current_thread())]
    assert track.readyState == "ended"
    await track.aclose()
    await track.aclose()
    assert sum(camera.disconnects for camera in camera_sdk.cameras) == (
        0 if stage in {"import", "config", "construct"} else 1
    )
    assert_worker_released(camera_sdk)


@pytest.mark.parametrize(
    "image,message",
    [
        (SimpleNamespace(shape=(18, 16, 3), dtype="uint8"), "configured height/width"),
        (SimpleNamespace(shape=(16, 16), dtype="uint8"), "RGB channels"),
        (SimpleNamespace(shape=(16, 16, 4), dtype="uint8"), "RGB channels"),
        (SimpleNamespace(shape=(16, 16, 3), dtype="float32"), "dtype uint8"),
        (None, "configured height/width"),
    ],
)
async def test_camera_rejects_unconfigured_modes_before_start_returns(camera_sdk, image, message):
    camera_sdk.images.append(image)
    track = LeRobotCameraTrack(0, VideoSettings(width=16, height=16))
    errors = []
    track.on_error = errors.append
    with pytest.raises(ValueError, match=message) as raised:
        await track.start()
    assert errors == [raised.value]
    assert camera_sdk.converted == []
    await track.aclose()
    assert camera_sdk.cameras[0].disconnects == 1
    assert_worker_released(camera_sdk)


async def test_camera_recv_failure_latches_stop_and_preserves_original_error(camera_sdk):
    track = LeRobotCameraTrack(0, VideoSettings(width=16, height=16))
    errors = []
    track.on_error = errors.append
    await track.start()
    error = TimeoutError("camera stopped delivering images")
    camera_sdk.actions["read"] = error
    with pytest.raises(TimeoutError) as raised:
        await track.recv()
    # asyncio may recreate the executor's TimeoutError while wrapping its future.
    assert str(raised.value) == str(error)
    assert errors == [raised.value]
    assert track.readyState == "ended"
    await track.aclose()
    await track.aclose()
    assert camera_sdk.cameras[0].disconnects == 1
    assert_worker_released(camera_sdk)


async def test_partial_setup_and_cleanup_errors_both_propagate(camera_sdk):
    setup_error = RuntimeError("partial camera connection")
    cleanup_error = RuntimeError("partial camera cleanup")
    camera_sdk.actions.update(connect=setup_error, disconnect=cleanup_error)
    track = LeRobotCameraTrack(0, VideoSettings(width=16, height=16))
    errors = []
    track.on_error = errors.append
    with pytest.raises(RuntimeError) as setup:
        await track.start()
    with pytest.raises(RuntimeError) as cleanup:
        await track.aclose()
    with pytest.raises(RuntimeError):
        await track.aclose()
    assert setup.value is setup_error
    assert cleanup.value is cleanup_error
    assert errors == [setup_error, cleanup_error]
    assert camera_sdk.cameras[0].disconnects == 1
    assert_worker_released(camera_sdk)


@pytest.mark.parametrize("stage", ["connect", "fourcc", "read", "convert"])
async def test_cancelled_start_joins_owned_work_before_owner_closes(camera_sdk, stage):
    entered, release = threading.Event(), threading.Event()

    def block():
        entered.set()
        assert release.wait(2)

    camera_sdk.actions[stage] = block
    track = LeRobotCameraTrack(
        0, VideoSettings(width=16, height=16, fourcc="MJPG" if stage == "fourcc" else None)
    )
    errors = []
    track.on_error = errors.append
    start = asyncio.create_task(track.start())
    try:
        await wait_until(entered.is_set)
        start.cancel()
        await asyncio.sleep(0)
        start.cancel()
        await asyncio.sleep(0)
        assert not start.done()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await start
        assert track.readyState == "ended"
        # stop/cancellation must not race camera joining ahead of arm shutdown.
        assert camera_sdk.cameras[0].disconnects == 0
        await track.aclose()
    assert errors == []
    assert camera_sdk.cameras[0].disconnects == 1
    assert_worker_released(camera_sdk)


@pytest.mark.parametrize("interruption", ["stop", "cancel"])
@pytest.mark.parametrize("read_fails", [False, True])
async def test_stop_or_cancel_does_not_mask_inflight_camera_failure(
    camera_sdk, interruption, read_fails
):
    entered, release = threading.Event(), threading.Event()
    error = RuntimeError("camera read failed while stopping")

    def block():
        entered.set()
        assert release.wait(2)
        if read_fails:
            raise error

    track = LeRobotCameraTrack(0, VideoSettings(width=16, height=16))
    errors = []
    track.on_error = errors.append
    await track.start()
    camera_sdk.actions["read"] = block
    receive = asyncio.create_task(track.recv())
    try:
        await wait_until(entered.is_set)
        if interruption == "stop":
            track.stop()
        else:
            receive.cancel()
        await asyncio.sleep(0)
        assert not receive.done()
    finally:
        release.set()
        expected = (
            RuntimeError
            if read_fails
            else (MediaStreamError if interruption == "stop" else asyncio.CancelledError)
        )
        with pytest.raises(expected):
            await receive
        await track.aclose()
    assert errors == ([error] if read_fails else [])
    assert_worker_released(camera_sdk)


async def test_aclose_joins_inflight_read_despite_repeated_cancellation(camera_sdk):
    entered, release = threading.Event(), threading.Event()

    def block():
        entered.set()
        assert release.wait(2)

    track = LeRobotCameraTrack(0, VideoSettings(width=16, height=16))
    await track.start()
    camera_sdk.actions["read"] = block
    receive = asyncio.create_task(track.recv())
    await wait_until(entered.is_set)
    close = asyncio.create_task(track.aclose())
    try:
        await asyncio.sleep(0)
        assert track.readyState == "ended"
        close.cancel()
        await asyncio.sleep(0)
        close.cancel()
        await asyncio.sleep(0)
        assert not close.done()
        assert camera_sdk.cameras[0].disconnects == 0
    finally:
        release.set()
        with pytest.raises(MediaStreamError):
            await receive
        with pytest.raises(asyncio.CancelledError):
            await close
        await track.aclose()
    assert camera_sdk.cameras[0].disconnects == 1
    assert_worker_released(camera_sdk)


@pytest.mark.parametrize("cancel_close", [False, True])
async def test_disconnect_failure_is_reported_once_even_during_cancel(camera_sdk, cancel_close):
    entered, release = threading.Event(), threading.Event()
    error = RuntimeError("disconnect failed")

    def fail_disconnect():
        entered.set()
        assert release.wait(2)
        raise error

    track = LeRobotCameraTrack(0, VideoSettings(width=16, height=16))
    errors = []
    track.on_error = errors.append
    await track.start()
    camera_sdk.actions["disconnect"] = fail_disconnect
    close = asyncio.create_task(track.aclose())
    try:
        await wait_until(entered.is_set)
        if cancel_close:
            close.cancel()
            await asyncio.sleep(0)
    finally:
        release.set()
        with pytest.raises(RuntimeError) as raised:
            await close
    assert raised.value is error
    with pytest.raises(RuntimeError) as repeated:
        await track.aclose()
    assert repeated.value is error
    assert errors == [error]
    assert camera_sdk.cameras[0].disconnects == 1
    assert_worker_released(camera_sdk)


async def test_camera_close_without_start_does_not_import_sdk_or_create_worker(monkeypatch):
    def forbidden_executor(*args, **kwargs):
        pytest.fail("Closing an unstarted camera must not allocate a worker")

    monkeypatch.setattr(video_sources, "ThreadPoolExecutor", forbidden_executor)
    track = LeRobotCameraTrack(0, VideoSettings())
    await track.aclose()
    await track.aclose()
    with pytest.raises(MediaStreamError):
        await track.start()
