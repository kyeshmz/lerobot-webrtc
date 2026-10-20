"""Latest-only decoded video ownership shared by SDK observations and browser viewing."""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Callable
from contextlib import suppress

from aiortc.mediastreams import MediaStreamError, MediaStreamTrack
from av import VideoFrame

from .video import MAX_VIDEO_HEIGHT, MAX_VIDEO_WIDTH, VideoSettings


class VideoReceiver:
    def __init__(self, settings: VideoSettings | None = None, frame_timeout: float = 3):
        if (
            isinstance(frame_timeout, bool)
            or not isinstance(frame_timeout, (int, float))
            or (not math.isfinite(frame_timeout) or frame_timeout <= 0)
        ):
            raise ValueError("Frame timeout must be finite and positive")
        self.settings = settings or VideoSettings()
        self.frame_timeout = frame_timeout
        self.on_error: Callable[[Exception], None] = lambda error: None
        self.started = asyncio.Event()
        self.ready = asyncio.Event()
        self._signaling_ready = asyncio.Event()
        self.frames_received = 0
        self.received_at = 0.0
        self._closing = False
        self._error: Exception | None = None
        self._source: MediaStreamTrack | None = None
        self._frame: VideoFrame | None = None
        self._generation = 0
        self._frame_changed = asyncio.Event()
        self._receive_task: asyncio.Task | None = None
        self._close_task: asyncio.Task | None = None

    @property
    def signaling_ready(self) -> asyncio.Event:
        return self._signaling_ready

    @property
    def frame(self) -> VideoFrame | None:
        return self._frame

    async def start(self) -> None:
        if self._closing:
            raise RuntimeError("Video receiver is closed")
        self.started.set()
        self._signaling_ready.set()

    def attach(self, track: MediaStreamTrack) -> None:
        if track.kind != "video":
            raise ValueError("Receiver accepts video only")
        if self._source is not None:
            raise ValueError("Receiver accepts only one source track")
        if self._closing or self._error is not None:
            raise RuntimeError("Video receiver is closed or failed")
        self._source = track
        track.on("ended", self._source_ended)
        self._receive_task = asyncio.create_task(self._receive(), name="video-receiver")

    def _source_ended(self) -> None:
        self._fail(ConnectionError("Remote video track ended unexpectedly"))

    async def _receive(self) -> None:
        frame = None
        try:
            while not self._closing and self._error is None:
                async with asyncio.timeout(self.frame_timeout):
                    frame = await self._source.recv()
                if self._closing or self._error is not None:
                    return
                if not isinstance(frame, VideoFrame) or not (
                    0 < frame.width <= MAX_VIDEO_WIDTH and 0 < frame.height <= MAX_VIDEO_HEIGHT
                ):
                    raise ValueError("Decoded video frame exceeds permitted dimensions")
                self._frame = frame
                self.received_at = time.monotonic()
                self._generation += 1
                self.frames_received += 1
                self.ready.set()
                self._frame_changed.set()
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            self._fail(TimeoutError("Remote video frame timeout"))
        except MediaStreamError:
            self._fail(ConnectionError("Remote video track ended unexpectedly"))
        except Exception as exc:
            self._fail(exc)
        finally:
            frame = None

    def _fail(self, error: Exception) -> None:
        if not self._closing and self._error is None:
            self._error = error
            self._frame = None
            self._frame_changed.set()
            with suppress(Exception):
                self.on_error(error)

    async def close(self) -> None:
        if self._close_task is None:
            self._closing = True
            self._close_task = asyncio.create_task(self._close_owned(), name="video-receiver-close")
        await asyncio.shield(self._close_task)

    async def _close_owned(self) -> None:
        if self._receive_task is not None:
            self._receive_task.cancel()
            await asyncio.gather(self._receive_task, return_exceptions=True)
        if self._source is not None:
            with suppress(Exception):
                self._source.remove_listener("ended", self._source_ended)
                self._source.stop()
            self._source = None
        self._frame = None
        self._frame_changed.set()
