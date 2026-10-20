"""Synchronous, single-controller native Robot transport, without SDK imports.

Only the owned asyncio thread touches the peer. Telemetry can advance in that
thread, but an SDK submission always consumes the sample pinned by its caller.
An uncertain submission is terminal, never queued for retry or reconnection.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import math
import os
import threading
import time
from dataclasses import dataclass, fields
from typing import TYPE_CHECKING

from aiortc import RTCIceServer

from .models import arm_model
from .native_options import RemoteRobotOptions
from .native_protocol import NATIVE_LABEL, decode_native, native_message, permit, sequence
from .peer import Peer
from .protocol import validate_action, validate_observation

if TYPE_CHECKING:
    import av


def _make_video_receiver(options: RemoteRobotOptions):
    from .video_receiver import VideoReceiver

    return VideoReceiver(settings=options.video_settings(), frame_timeout=options.video_timeout)


def _credentials(options: RemoteRobotOptions) -> tuple[str, list[RTCIceServer]]:
    token = os.environ.get(options.token_env, "")
    if len(token) < 16 or any(not 32 <= ord(char) <= 126 for char in token):
        raise ValueError("Native Robot token must contain at least 16 printable ASCII characters")
    servers = [RTCIceServer(urls=url) for url in options.stun]
    if options.turn is not None:
        username = os.environ.get(options.turn_username_env, "")
        password = os.environ.get(options.turn_password_env, "")
        if not username or not password:
            raise ValueError("Native Robot TURN credentials are not configured")
        servers.append(RTCIceServer(urls=options.turn, username=username, credential=password))
    return token, servers


@dataclass(frozen=True)
class _Sample:
    seq: int
    position: dict[str, float]
    permit: str
    received_at: float


@dataclass
class _Submission:
    seq: int
    action: dict[str, float]
    permit: str
    deadline: float
    result: concurrent.futures.Future
    sent: bool = False


class RemoteRobotClient:
    """A single-use connection; explicit reconnection needs a new client.

    Observation/action calls belong to one controlling thread. Frame reads and
    connection status are passive and may be used from other threads. Constructing
    this object neither resolves credentials nor starts threads or network I/O.
    """

    def __init__(self, options: RemoteRobotOptions):
        self.options = options
        self._lock = threading.RLock()
        self._join_lock = threading.Lock()
        self._closed = threading.Event()
        self._connected_result: concurrent.futures.Future = concurrent.futures.Future()
        self._started = False
        self._terminal = False
        self._connected = False
        self._error: Exception | None = None
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop: asyncio.Event | None = None
        self._first_state: asyncio.Event | None = None
        self._peer: Peer | None = None
        self._receiver = None
        self._settings: RemoteRobotOptions | None = None
        self._session: str | None = None
        self._hello: tuple | None = None
        self._simulated: bool | None = None
        self._host_timeout = 0.0
        self._sample: _Sample | None = None
        self._pin: _Sample | None = None
        self._controller: threading.Thread | None = None
        self._next_action = 0
        self._last_result = -1
        self._pending: _Submission | None = None

    @property
    def is_connected(self) -> bool:
        with self._lock:
            return self._connected and not self._terminal

    @property
    def simulated(self) -> bool:
        with self._lock:
            self._require_connection()
            assert self._simulated is not None
            return self._simulated

    def connect(self) -> None:
        try:
            with self._lock:
                if self._started or self._terminal:
                    raise RuntimeError("Native Robot clients are single-use")
                self._started = True
                # Freeze the public, mutable config, including its list of URLs.
                values = {
                    field.name: getattr(self.options, field.name)
                    for field in fields(RemoteRobotOptions)
                }
                values["stun"] = list(values["stun"])
                self._settings = RemoteRobotOptions(**values)
                token, servers = _credentials(self._settings)
                # SDK commands do not pass through our standalone CLI entrypoint.
                for name in ("aiortc", "aioice", "aiohttp"):
                    logger = logging.getLogger(name)
                    logger.setLevel(max(logging.WARNING, logger.getEffectiveLevel()))
                self._thread = threading.Thread(
                    target=self._thread_main,
                    args=(token, servers),
                    name="native-robot-network",
                    daemon=False,
                )
                try:
                    self._thread.start()
                except BaseException:
                    self._thread = None
                    raise
            self._connected_result.result(timeout=self._settings.connect_timeout)
        except BaseException:
            self.disconnect()
            raise

    def disconnect(self) -> None:
        """Revoke submissions, send best-effort STOP, then join owned cleanup.

        Joining deliberately has no deadline: a timed-out SDK call must not leave
        behind a live network thread that can later dispatch its command.
        """
        self._terminate(ConnectionError("Native Robot disconnected"))
        interrupted: BaseException | None = None
        with self._join_lock:
            thread = self._thread
            if thread is not None and thread is not threading.current_thread():
                while not self._closed.is_set():
                    try:
                        self._closed.wait()
                    except BaseException as exc:
                        interrupted = exc
                thread.join()
        if interrupted is not None:
            raise interrupted

    def get_observation(self) -> dict[str, float]:
        try:
            with self._lock:
                self._require_controller()
                self._require_connection()
                if self._pending is not None:
                    raise RuntimeError("A native Robot action is already outstanding")
                sample = self._sample
                if sample is None or time.monotonic() >= self._sample_deadline(sample):
                    raise TimeoutError("Native Robot measured observation expired")
                self._pin = sample
                return sample.position.copy()
        except BaseException:
            self.disconnect()
            raise

    def send_action(self, action: dict[str, float]) -> dict[str, float]:
        try:
            with self._lock:
                self._require_controller()
                self._require_connection()
                if self._pending is not None:
                    raise RuntimeError("A native Robot action is already outstanding")
                sample, self._pin = self._pin, None
                if sample is None:
                    raise RuntimeError("Native Robot send_action requires a fresh get_observation")
                now = time.monotonic()
                deadline = self._sample_deadline(sample)
                if now >= deadline:
                    raise TimeoutError("Native Robot pinned observation expired")
                assert self._settings is not None
                pending = _Submission(
                    sequence(self._next_action),
                    validate_action(action, self._settings.model),
                    sample.permit,
                    min(deadline, now + self._settings.request_timeout),
                    concurrent.futures.Future(),
                )
                self._next_action += 1
                self._pending = pending
                assert self._loop is not None
                self._loop.call_soon_threadsafe(self._dispatch, pending)
            return pending.result.result(timeout=max(0, pending.deadline - time.monotonic()))
        except BaseException:
            # Latch BEFORE waiting for loop cleanup, so a delayed dispatch cannot run.
            self.disconnect()
            raise

    def get_frame(self) -> av.VideoFrame:
        with self._lock:
            self._require_connection()
            assert self._settings is not None
            if self._settings.camera_name is None:
                raise RuntimeError("No native Robot camera is configured")
            result: concurrent.futures.Future = concurrent.futures.Future()
            loop = self._loop
            timeout = self._settings.request_timeout
        try:
            assert loop is not None
            loop.call_soon_threadsafe(self._read_frame, result)
            return result.result(timeout=timeout)
        except BaseException:
            self.disconnect()
            raise

    def _require_connection(self) -> None:
        if not self._connected or self._terminal:
            raise self._error or ConnectionError("Native Robot is not connected")

    def _require_controller(self) -> None:
        current = threading.current_thread()
        if self._controller is None:
            self._controller = current
        elif self._controller is not current:
            raise RuntimeError(
                "Native Robot observation/action calls require one controlling thread"
            )

    def _sample_deadline(self, sample: _Sample) -> float:
        assert self._settings is not None
        # This is a conservative LOCAL age bound, not a trusted remote clock or
        # proof of remaining permit lifetime. The host independently enforces it.
        return sample.received_at + min(self._settings.observation_timeout, self._host_timeout)

    def _terminate(self, error: Exception) -> None:
        with self._lock:
            self._terminal = True
            self._connected = False
            self._pin = None
            if self._error is None:
                self._error = error
            if not self._connected_result.done():
                self._connected_result.set_exception(self._error)
            if self._pending is not None and not self._pending.result.done():
                self._pending.result.set_exception(self._error)
            loop, stop = self._loop, self._stop
        if loop is not None and stop is not None:
            try:
                loop.call_soon_threadsafe(stop.set)
            except RuntimeError:
                pass  # The owned loop has already finished closing.

    def _thread_main(self, token: str, servers: list[RTCIceServer]) -> None:
        try:
            asyncio.run(self._run(token, servers))
        except BaseException:
            # Third-party exception strings can contain resolved credentials.
            self._terminate(ConnectionError("Native Robot network failed"))
        finally:
            with self._lock:
                self._loop = None
                self._peer = None
                self._receiver = None
            self._closed.set()

    async def _run(self, token: str, servers: list[RTCIceServer]) -> None:
        tasks = []
        peer = None
        receiver = None
        try:
            with self._lock:
                self._loop = asyncio.get_running_loop()
                self._stop = asyncio.Event()
                self._first_state = asyncio.Event()
                if self._terminal:
                    return
            assert self._settings is not None
            options = self._settings
            receiver = _make_video_receiver(options) if options.camera_name is not None else None
            self._receiver = receiver
            peer = Peer(
                "leader",
                options.signaling,
                options.room,
                token,
                servers,
                channel_label=NATIVE_LABEL,
                video_receiver=receiver,
            )
            self._peer = peer
            peer.on_message = self._on_message
            tasks = [
                asyncio.create_task(self._monitor(peer)),
                asyncio.create_task(self._establish(peer)),
            ]
            await self._stop.wait()
        except BaseException:
            self._terminate(ConnectionError("Native Robot network failed"))
        finally:
            self._terminate(ConnectionError("Native Robot connection ended"))
            # STOP precedes cancellation of setup, whose own cleanup may close Peer.
            if peer is not None and self._session is not None:
                try:
                    peer.send(native_message(self._settings.model, self._session, "stop"))
                except Exception:
                    pass
            await self._finish_close(self._cleanup(peer, receiver, tasks))

    @staticmethod
    async def _cleanup(peer, receiver, tasks) -> None:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if peer is not None:
            await peer.close()
        elif receiver is not None:
            # Constructor failure occurred before Peer could take ownership.
            await receiver.close()

    @staticmethod
    async def _finish_close(operation) -> None:
        closing = asyncio.create_task(operation)
        while not closing.done():
            try:
                await asyncio.shield(closing)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        # Do not propagate third-party exceptions (which may expose credentials).
        try:
            closing.result()
        except BaseException:
            pass

    async def _establish(self, peer: Peer) -> None:
        try:
            assert self._settings is not None and self._first_state is not None
            async with asyncio.timeout(self._settings.connect_timeout):
                await peer.connect(timeout=self._settings.connect_timeout)
                await self._first_state.wait()
                if self._receiver is not None:
                    await self._receiver.ready.wait()
                with self._lock:
                    if self._terminal:
                        return
                    if self._sample is None or (
                        time.monotonic() >= self._sample_deadline(self._sample)
                    ):
                        raise TimeoutError
                    self._connected = True
                    self._connected_result.set_result(None)
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            self._terminate(TimeoutError("Native Robot connection timed out"))
        except Exception:
            self._terminate(ConnectionError("Native Robot setup failed"))

    async def _monitor(self, peer: Peer) -> None:
        try:
            while not self._terminal:
                if peer.closed.is_set():
                    self._terminate(ConnectionError("Native Robot peer closed"))
                    return
                with self._lock:
                    if not self._terminal and self._session is not None and self._sample is None:
                        peer.send(
                            native_message(
                                self._settings.model,
                                self._session,
                                "ready",
                                allow_physical=self._settings.allow_physical,
                            )
                        )
                try:
                    async with asyncio.timeout(0.02):
                        await self._stop.wait()
                except TimeoutError:
                    pass
        except asyncio.CancelledError:
            raise
        except Exception:
            self._terminate(ConnectionError("Native Robot network failed"))

    def _on_message(self, message: str | bytes) -> None:
        try:
            with self._lock:
                if self._terminal:
                    return
                data = decode_native(message, self._settings.model, self._session)
                kind = data["type"]
                if kind == "hello":
                    self._receive_hello(data)
                elif self._session is None:
                    raise ValueError("Native Robot state preceded handshake")
                elif kind == "state":
                    seq = sequence(data.get("seq"))
                    position = validate_observation(data.get("position"), self._settings.model)
                    token = permit(data.get("permit"))
                    if self._sample is None or seq > self._sample.seq:
                        self._sample = _Sample(seq, position, token, time.monotonic())
                        self._first_state.set()
                elif kind == "result":
                    seq = sequence(data.get("seq"))
                    action = validate_action(data.get("action"), self._settings.model)
                    if seq <= self._last_result:
                        return  # Validated duplicate of an already completed command.
                    pending = self._pending
                    if pending is None or not pending.sent or seq != pending.seq:
                        raise ValueError("Unmatched native Robot result")
                    if time.monotonic() >= pending.deadline:
                        raise ValueError("Late native Robot result")
                    self._last_result = seq
                    self._pending = None
                    pending.result.set_result(action)
                else:
                    raise ValueError("Unexpected native Robot message")
        except Exception:
            self._terminate(ConnectionError("Invalid native Robot handshake, state or result"))

    def _receive_hello(self, data: dict) -> None:
        options = self._settings
        if data.get("joints") != list(arm_model(options.model).joints):
            raise ValueError("Native Robot joint order differs")
        simulated = data.get("simulated")
        robot_id = data.get("robot_id")
        timeout = data.get("timeout")
        if type(simulated) is not bool or (
            not isinstance(robot_id, str)
            or not 1 <= len(robot_id) <= 128
            or (simulated and robot_id != "simulated")
            or (not simulated and robot_id == "simulated")
        ):
            raise ValueError("Invalid native Robot identity or simulation marker")
        if not simulated and not options.allow_physical:
            raise ValueError("Physical native Robot permission was not granted")
        if options.remote_id is not None and options.remote_id != robot_id:
            raise ValueError("Native Robot remote identity differs")
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (float, int))
            or (not math.isfinite(timeout) or timeout <= 0)
        ):
            raise ValueError("Invalid native Robot watchdog timeout")
        if "video" not in data:
            raise ValueError("Missing native Robot camera metadata")
        video = data["video"]
        expected = (
            None
            if options.camera_name is None
            else {
                "width": options.video_width,
                "height": options.video_height,
                "fps": options.video_fps,
            }
        )
        if video != expected or (
            video is not None
            and (
                not isinstance(video, dict)
                or any(type(value) is not int for value in video.values())
            )
        ):
            raise ValueError("Native Robot camera dimensions or FPS differ")
        identity = (simulated, robot_id, float(timeout), video)
        if self._hello is not None and self._hello != identity:
            raise ValueError("Native Robot hello changed during session")
        self._hello = identity
        self._session = data["session"]
        self._simulated = simulated
        self._host_timeout = float(timeout)

    def _dispatch(self, pending: _Submission) -> None:
        try:
            with self._lock:
                if self._terminal or self._pending is not pending:
                    return
                if time.monotonic() >= pending.deadline:
                    raise TimeoutError
                pending.sent = True
                if not self._peer.send(
                    native_message(
                        self._settings.model,
                        self._session,
                        "action",
                        seq=pending.seq,
                        permit=pending.permit,
                        action=pending.action,
                    )
                ):
                    raise ConnectionError
        except Exception:
            self._terminate(ConnectionError("Native Robot action was not confirmed"))

    def _read_frame(self, result: concurrent.futures.Future) -> None:
        try:
            with self._lock:
                self._require_connection()
                receiver = self._receiver
                frame, received_at = receiver.frame, receiver.received_at
                now = time.monotonic()
                if (
                    frame is None
                    or not isinstance(received_at, (int, float))
                    or (
                        not math.isfinite(received_at)
                        or not 0 <= now - received_at < self._settings.video_timeout
                    )
                ):
                    raise TimeoutError("Native Robot camera frame expired")
                if (frame.width, frame.height) != (
                    self._settings.video_width,
                    self._settings.video_height,
                ):
                    raise ValueError("Native Robot camera frame dimensions differ")
                result.set_result(frame)
        except Exception as exc:
            result.set_exception(exc)
