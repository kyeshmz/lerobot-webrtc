"""Reuse LeRobot's hardware drivers; keep serial I/O off the asyncio event loop."""

from __future__ import annotations

import asyncio
import logging
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from .protocol import JOINTS, validate_action

logger = logging.getLogger(__name__)


class LeRobotArm:
    def __init__(self, role: str, model: str, port: str, arm_id: str, delta: float, exit_mode: str):
        try:
            if role == "leader":
                from lerobot.teleoperators import so_leader as module
            else:
                from lerobot.robots import so_follower as module
        except ImportError as exc:
            raise RuntimeError(
                'Install hardware support: pip install -e ".[hardware]" (Python 3.12+)'
            ) from exc
        prefix = "SO101" if model == "so101" else "SO100"
        suffix = "Leader" if role == "leader" else "Follower"
        config_cls = getattr(module, prefix + suffix + "Config")
        device_cls = getattr(module, prefix + suffix)
        options = dict(port=port, id=arm_id, use_degrees=False)
        if role == "follower":
            options.update(
                max_relative_target=delta,
                disable_torque_on_disconnect=exit_mode == "torque-off",
            )
        self.device = device_cls(config_cls(**options))
        self.role = role

    def connect(self) -> None:
        # Never enter an interactive calibration procedure during a network session.
        if not self.device.calibration:
            raise RuntimeError("No saved calibration. Run lerobot-webrtc calibrate first.")
        self.device.connect(calibrate=False)
        if not self.device.is_calibrated:
            raise RuntimeError("Motor calibration mismatch. Run lerobot-webrtc calibrate first.")

    def calibrate(self) -> None:
        self.device.connect(calibrate=False)
        self.device.calibrate()

    def get_action(self) -> dict[str, float]:
        return validate_action(self.device.get_action())

    def send_action(self, action: dict[str, float]) -> dict[str, float]:
        return self.device.send_action(action)

    def hold(self) -> None:
        if not self.device.is_connected or not self.device.is_calibrated:
            return
        observation = self.device.get_observation()
        self.device.send_action({joint: observation[joint] for joint in JOINTS})

    def disconnect(self) -> None:
        # Also clean up when connect() failed after opening the serial bus.
        if self.device.bus.is_connected:
            self.device.disconnect()


class SimulatedArm:
    """A synthetic leader and a rate-limited in-memory follower, no LeRobot import."""

    def __init__(self, delta: float = 5.0):
        self.delta = delta
        self.position = dict.fromkeys(JOINTS, 0.0)
        self.applied = 0
        self.connected = False
        self.held = False

    def connect(self) -> None:
        self.connected = True

    def get_action(self) -> dict[str, float]:
        action = dict.fromkeys(JOINTS, 0.0)
        action["shoulder_pan.pos"] = 15 * math.sin(time.monotonic())
        action["gripper.pos"] = 25
        return action

    def send_action(self, action: dict[str, float]) -> dict[str, float]:
        for joint, target in validate_action(action).items():
            current = self.position[joint]
            self.position[joint] = current + max(-self.delta, min(self.delta, target - current))
        self.applied += 1
        return self.position.copy()

    def hold(self) -> None:
        self.held = True

    def disconnect(self) -> None:
        self.connected = False


class AsyncArm:
    """One worker owns all hardware access, including shutdown.

    Cancellation waits for the current serial operation before allowing cleanup;
    it cannot leave an abandoned motor command racing with disconnect().
    """

    def __init__(self, arm: LeRobotArm | SimulatedArm, role: str):
        self.arm = arm
        self.role = role
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="arm")
        self._close_task = None
        self._state_lock = threading.Lock()
        self._closing = False

    async def _wait_owned(self, future):
        """Finish an executor operation despite repeated cancellation."""
        cancelled = False
        while not future.done():
            try:
                await asyncio.shield(future)
            except asyncio.CancelledError:
                cancelled = True
        if cancelled:
            try:
                future.result()
            except Exception:
                logger.exception("Hardware operation failed during cancellation")
            raise asyncio.CancelledError
        return future.result()

    async def call(self, method: str, *args):
        concurrent_future = self.executor.submit(getattr(self.arm, method), *args)
        future = asyncio.wrap_future(concurrent_future)
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            concurrent_future.cancel()
            await self._wait_owned(future)
            raise

    async def send(
        self,
        action: dict[str, float],
        deadline: float,
        session_active=lambda: True,
    ) -> bool:
        with self._state_lock:
            if self._closing:
                return False

        def guarded_send():
            with self._state_lock:
                if self._closing or time.monotonic() >= deadline or not session_active():
                    return False
            self.arm.send_action(action)
            return True

        # Keep admission and submission atomic with close() latching shutdown.
        with self._state_lock:
            if self._closing:
                return False
            concurrent_future = self.executor.submit(guarded_send)
        future = asyncio.wrap_future(concurrent_future)
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            # Revoke work that has not begun; a running serial call must be joined.
            concurrent_future.cancel()
            await self._wait_owned(future)
            raise

    async def close(self, hold: bool = True) -> None:
        with self._state_lock:
            self._closing = True
            if self._close_task is None:
                self._close_task = asyncio.create_task(self._close_owned(hold))
        cancelled = False
        while not self._close_task.done():
            try:
                await asyncio.shield(self._close_task)
            except asyncio.CancelledError:
                cancelled = True
        # Propagate a cleanup failure even when cancellation was also requested.
        self._close_task.result()
        if cancelled:
            raise asyncio.CancelledError

    async def _close_owned(self, hold: bool) -> None:
        errors = []
        if hold and self.role == "follower":
            try:
                await self.call("hold")
            except Exception as exc:
                logger.exception("Could not hold the follower; use the physical emergency stop")
                errors.append(exc)
        try:
            await self.call("disconnect")
        except Exception as exc:
            logger.exception("Could not disconnect the arm cleanly")
            errors.append(exc)
        finally:
            self.executor.shutdown(wait=True)
        if errors:
            raise ExceptionGroup("Arm cleanup did not reach requested shutdown state", errors)
