"""Fail-closed native Robot RPC, separate from standalone teleoperation."""

from __future__ import annotations

import asyncio
import math
import secrets
import sys
import time
from dataclasses import dataclass

from .arms import AsyncArm, SimulatedArm
from .b601 import B601Arm
from .models import arm_model
from .native_protocol import NATIVE_LABEL, decode_native, native_message, permit, sequence
from .peer import Peer
from .protocol import validate_action, validate_observation


@dataclass(frozen=True)
class _Action:
    seq: int
    action: dict[str, float]
    deadline: float


class _HostSession:
    """Own the epoch, single action slot, and follower-clock freshness grants."""

    def __init__(self, arm, peer, model, timeout, startup_timeout, robot_id):
        self.arm = arm
        self.peer = peer
        self.model = model
        self.timeout = timeout
        self.startup_timeout = startup_timeout
        self.session = secrets.token_hex(16)
        self.ready = asyncio.Event()
        self.permission = None
        self.error = None
        self.stop_requested = False
        self.watchdog_deadline = None
        self.pending = None
        self.last_sequence = -1
        self.state_sequence = 0
        self.permits = {}
        self.last_result = None
        self.simulated = isinstance(arm.arm, SimulatedArm)
        source = getattr(peer, "video_source", None)
        video = (
            None
            if source is None
            else {key: getattr(source.settings, key) for key in ("width", "height", "fps")}
        )
        self.hello = native_message(
            model,
            self.session,
            "hello",
            joints=list(arm_model(model).joints),
            simulated=self.simulated,
            robot_id=robot_id,
            timeout=timeout,
            video=video,
        )

    def check_active(self, now=None):
        if self.peer.closed.is_set():
            raise self.error or self.peer.error or ConnectionError("Native Robot session stopped")
        now = time.monotonic() if now is None else now
        if self.watchdog_deadline is not None and now >= self.watchdog_deadline:
            raise TimeoutError("Native Robot action watchdog expired; restart both peers")
        return now

    def receive(self, message):
        if self.peer.closed.is_set():
            return
        try:
            # A late packet must never revive an already-expired action watchdog.
            now = self.check_active()
            data = decode_native(message, self.model, self.session)
            kind = data["type"]
            if kind == "stop":
                self.stop_requested = True
                self.peer.closed.set()
            elif kind == "ready":
                allowed = data.get("allow_physical")
                if type(allowed) is not bool or (not self.simulated and not allowed):
                    raise ValueError("Physical native Robot requires allow_physical=true")
                if self.ready.is_set() and allowed != self.permission:
                    raise ValueError("Native Robot ready permission changed")
                # Repeated identical ready messages during a slow connect are harmless;
                # they do not create permits or extend any motion deadline.
                self.permission = allowed
                self.ready.set()
            elif kind == "action":
                if self.watchdog_deadline is None or self.pending is not None:
                    raise ValueError("Native Robot action arrived before state or overlapped")
                seq = sequence(data.get("seq"))
                if seq <= self.last_sequence:
                    raise ValueError("Out-of-sequence native Robot action")
                token = permit(data.get("permit"))
                deadline = self.permits.get(token, 0)
                if now >= deadline:
                    raise TimeoutError("Native Robot action permit expired or is unknown")
                action = validate_action(data.get("action"), self.model)
                self.pending = _Action(seq, action, deadline)
                self.last_sequence = seq
                self.last_result = None
            else:
                raise ValueError("Unexpected native Robot message at host")
        except Exception as exc:
            self.error = exc
            self.peer.closed.set()

    async def run(self, fps):
        async with asyncio.timeout(self.startup_timeout):
            while not self.ready.is_set():
                self.check_active()
                self.peer.send(self.hello)
                if not self.ready.is_set():
                    await asyncio.sleep(min(0.05, 1 / fps))
        self.check_active()
        await self.arm.call("connect")
        self.check_active()
        self.watchdog_deadline = time.monotonic() + self.startup_timeout
        while True:
            tick = self.check_active()
            command = self.pending
            if command is not None:
                # Capture BOTH deadlines before the worker can queue behind I/O.
                deadline = min(command.deadline, self.watchdog_deadline)
                result = await self.arm.send_with_result(
                    command.action,
                    deadline,
                    session_active=lambda: not self.peer.closed.is_set(),
                )
                applied_at = self.check_active()
                if not result.accepted or applied_at >= deadline:
                    raise TimeoutError("Native Robot action overran its dispatch deadline")
                actual = validate_action(result.action, self.model)
                receipt = native_message(
                    self.model,
                    self.session,
                    "result",
                    seq=command.seq,
                    action=actual,
                )
                if self.check_active() >= deadline:
                    raise TimeoutError("Native Robot action result exceeded its dispatch deadline")
                self.last_result = receipt
                self.pending = None
                # Only a confirmed, timely driver result renews the active watchdog.
                self.watchdog_deadline = applied_at + self.timeout
            if self.last_result is not None:
                # The data channel is unreliable: resend the same receipt, never motion.
                self.peer.send(self.last_result)
            self.check_active()
            position = validate_observation(await self.arm.call("get_observation"), self.model)
            observed_at = self.check_active()
            self.permits = {key: end for key, end in self.permits.items() if end > observed_at}
            token = secrets.token_hex(16)
            self.permits[token] = observed_at + self.timeout
            self.peer.send(
                native_message(
                    self.model,
                    self.session,
                    "state",
                    seq=self.state_sequence,
                    position=position,
                    permit=token,
                )
            )
            self.state_sequence += 1
            self.check_active()
            await asyncio.sleep(
                max(
                    0,
                    min(
                        1 / fps - (time.monotonic() - tick),
                        self.watchdog_deadline - time.monotonic(),
                    ),
                )
            )


async def _shutdown(arm, peer, tasks):
    await asyncio.gather(*tasks, return_exceptions=True)
    errors = []
    # Hardware is held/disconnected before network or media cleanup can block.
    for close in (arm.close, peer.close):
        try:
            await close()
        except BaseException as exc:
            errors.append(exc)
    return errors


async def run_robot_host(
    arm: AsyncArm,
    peer: Peer,
    model: str,
    fps: float,
    timeout: float,
    duration: float | None,
    *,
    robot_id: str = "simulated",
    startup_timeout: float = 5,
) -> None:
    """Serve one explicitly armed native Robot session; never reconnect or replay."""
    tasks = set()
    try:
        arm_model(model)
        device_model = "b601-rs" if isinstance(arm.arm, B601Arm) else arm.arm.model
        if device_model != model or arm.role != "follower" or peer.role != "follower":
            raise ValueError("Native Robot host requires a matching follower arm and peer")
        if getattr(peer, "channel_label", NATIVE_LABEL) != NATIVE_LABEL:
            raise ValueError("Native Robot host requires the native data channel label")
        for value in (fps, timeout, startup_timeout) + (() if duration is None else (duration,)):
            if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
                raise ValueError("Native Robot rates and deadlines must be finite and positive")
        if not isinstance(robot_id, str) or not robot_id:
            raise ValueError("Native Robot host requires a robot ID")
        session = _HostSession(arm, peer, model, timeout, startup_timeout, robot_id)
        peer.on_message = session.receive
        await peer.connect()
        tasks = {
            asyncio.create_task(session.run(fps), name="native-robot-host"),
            asyncio.create_task(peer.closed.wait()),
        }
        if duration is not None:
            tasks.add(asyncio.create_task(asyncio.sleep(duration)))
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            try:
                task.result()
            except ConnectionError:
                if not session.stop_requested:
                    raise
        if peer.closed.is_set() and not session.stop_requested:
            raise session.error or peer.error or ConnectionError("Native Robot peer disconnected")
    finally:
        # Latch revocation BEFORE cancelling/joining any in-flight motor operation.
        peer.closed.set()
        for task in tasks:
            task.cancel()
        primary = sys.exception()
        cleanup = asyncio.create_task(_shutdown(arm, peer, tasks))
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError as exc:
                if primary is None:
                    primary = exc
        errors = cleanup.result()
        if errors:
            raise BaseExceptionGroup(
                "Native Robot session failed during shutdown",
                ([primary] if primary is not None else []) + errors,
            )
        if isinstance(primary, asyncio.CancelledError):
            raise primary
