"""Explicitly started video sources, independent of robot and motor I/O."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from fractions import Fraction
from math import ceil

from aiortc import VideoStreamTrack
from aiortc.mediastreams import MediaStreamError
from av import VideoFrame

from .video import VideoSettings

logger = logging.getLogger(__name__)
VIDEO_TIME_BASE = Fraction(1, 90000)


async def _join_owned(future):
    """Join owned work even on repeated cancellation; real failures take priority."""
    cancelled = False
    while not future.done():
        try:
            await asyncio.shield(future)
        except asyncio.CancelledError:
            cancelled = True
        except Exception:
            break
    result = future.result()
    if cancelled:
        raise asyncio.CancelledError
    return result


class _PacedVideoTrack(VideoStreamTrack):
    def __init__(self, settings: VideoSettings):
        super().__init__()
        self.settings = settings
        self.on_error: Callable[[Exception], None] = lambda error: None
        self._ended = asyncio.Event()
        self.on("ended", self._ended.set)
        self._operation_lock = asyncio.Lock()
        self._epoch: float | None = None
        self._last_sent: float | None = None
        self._last_pts = -1

    def _require_live(self) -> None:
        if self.readyState != "live":
            raise MediaStreamError

    def _fail(self, error: Exception) -> None:
        # Latch the real control failure before emitting the generic media EOF.
        try:
            self.on_error(error)
        except Exception:
            logger.exception("Video error callback failed")
        self.stop()

    async def _pace(self) -> None:
        self._require_live()
        if self._last_sent is not None:
            delay = self._last_sent + 1 / self.settings.fps - time.monotonic()
            if delay > 0:
                try:
                    async with asyncio.timeout(delay):
                        await self._ended.wait()
                except TimeoutError:
                    pass
        self._require_live()

    def _stamp(self, frame: VideoFrame) -> VideoFrame:
        now = time.monotonic()
        if self._epoch is None:
            self._epoch = now
        # Use elapsed monotonic time, not a queue of overdue frame deadlines.
        frame.pts = max(self._last_pts + 1, round((now - self._epoch) * 90000))
        frame.time_base = VIDEO_TIME_BASE
        self._last_pts = frame.pts
        self._last_sent = now
        return frame


class SyntheticVideoTrack(_PacedVideoTrack):
    """Changing YUV test pattern; needs no optional image or hardware packages."""

    def __init__(self, settings: VideoSettings):
        super().__init__(settings)
        self._phase = 0

    async def start(self) -> None:
        self._require_live()

    async def recv(self) -> VideoFrame:
        async with self._operation_lock:
            await self._pace()
            try:
                frame = VideoFrame(self.settings.width, self.settings.height, format="yuv420p")
                for index, plane in enumerate(frame.planes):
                    low, span = (16, 220) if index == 0 else (64, 128)
                    row = bytes(
                        low + (x * span // plane.width + self._phase * 7 + index * 43) % span
                        for x in range(plane.width)
                    )
                    # PyAV can pad rows, including at valid non-multiple-of-16 sizes.
                    plane.update((row + bytes(plane.line_size - plane.width)) * plane.height)
                self._phase += 1
                return self._stamp(frame)
            except Exception as exc:
                self._fail(exc)
                raise

    async def aclose(self) -> None:
        self.stop()


class LeRobotCameraTrack(_PacedVideoTrack):
    """One dedicated worker owns SDK imports, camera I/O, and RGB conversion.

    Cancellation joins an in-flight operation before propagating. The owner must
    always call ``aclose()`` (after stopping the arm), including after failed or
    cancelled startup. Inherited ``stop()`` only latches the track ended.
    """

    def __init__(self, index: int, settings: VideoSettings):
        super().__init__(settings)
        self.index = index
        self._executor: ThreadPoolExecutor | None = None
        self._camera = None
        self._started = False
        self._close_task: asyncio.Task | None = None

    async def _run_worker(self, operation):
        assert self._executor is not None
        try:
            future = asyncio.get_running_loop().run_in_executor(self._executor, operation)
            return await _join_owned(future)
        except Exception as exc:
            # This also reports a real read failure racing intentional stop or
            # cancellation, rather than replacing it with a normal media EOF.
            self._fail(exc)
            raise

    def _open_and_prime(self) -> None:
        from lerobot.cameras.opencv import OpenCVCamera, OpenCVCameraConfig

        config = OpenCVCameraConfig(
            index_or_path=self.index,
            width=self.settings.width,
            height=self.settings.height,
            fps=self.settings.fps,
            fourcc=self.settings.fourcc,
        )
        self._camera = OpenCVCamera(config)
        self._camera.connect(warmup=False)
        self._verify_fourcc()
        self._read_frame(startup=True)

    def _verify_fourcc(self) -> None:
        expected = self.settings.fourcc
        if expected is None:
            return
        # The pinned SDK only warns when a backend ignores its FOURCC request.
        # Keep this adapter-specific read-back on the same camera worker.
        try:
            import cv2

            value = self._camera.videocapture.get(cv2.CAP_PROP_FOURCC)
            code = int(value)
            if code != value or not 0 <= code <= 0xFFFFFFFF:
                raise ValueError("Invalid capture FOURCC value")
            actual = "".join(chr((code >> (8 * index)) & 0xFF) for index in range(4))
        except Exception as exc:
            raise ValueError(f"Cannot verify requested camera FOURCC {expected}") from exc
        if actual != expected:
            raise ValueError(f"Camera FOURCC {actual!r} does not match requested {expected}")

    def _read_frame(self, *, startup: bool = False) -> VideoFrame:
        # A new capture can take a full frame period, including the first recv
        # after priming. Allow bounded scheduling jitter, not repeated polling.
        timeout_ms = ceil(1000 / self.settings.fps) + 200
        if startup:
            # Capture-thread initialization may exceed one fast frame period.
            timeout_ms = max(1000, timeout_ms)
        image = self._camera.async_read(timeout_ms=timeout_ms)
        if getattr(image, "shape", None) != (self.settings.height, self.settings.width, 3):
            raise ValueError("Camera did not return the configured height/width and RGB channels")
        if getattr(image, "dtype", None) != "uint8":
            raise ValueError("Camera RGB frames must have dtype uint8")
        return VideoFrame.from_ndarray(image, format="rgb24")

    def _disconnect(self) -> None:
        # Clear before calling: disconnect is not idempotent, even if it fails.
        camera, self._camera = self._camera, None
        if camera is not None:
            camera.disconnect()

    async def start(self) -> None:
        async with self._operation_lock:
            self._require_live()
            if self._started:
                return
            self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="video-camera")
            try:
                await self._run_worker(self._open_and_prime)
            except asyncio.CancelledError:
                self.stop()
                raise
            self._require_live()
            self._started = True

    async def recv(self) -> VideoFrame:
        await self.start()
        async with self._operation_lock:
            await self._pace()
            frame = await self._run_worker(self._read_frame)
            self._require_live()
            return self._stamp(frame)

    async def aclose(self) -> None:
        self.stop()
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close_owned())
        await _join_owned(self._close_task)

    async def _close_owned(self) -> None:
        async with self._operation_lock:
            if self._executor is not None:
                try:
                    await self._run_worker(self._disconnect)
                finally:
                    # All queued work and SDK capture-thread joining finished on
                    # our worker; this only releases the now-idle executor.
                    self._executor.shutdown(wait=True)
                    self._executor = None
