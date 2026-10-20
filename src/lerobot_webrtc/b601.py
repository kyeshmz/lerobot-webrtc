"""B601-RS lifecycle around LeRobot's degree-position driver, never a servo reimplementation."""

from __future__ import annotations

import json
import math
import os
import re
import stat
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

from .models import arm_model
from .protocol import hold_action, validate_action, validate_observation

MODEL = "b601-rs"
FEEDBACK_TIMEOUT = 0.15
MAX_BENCH_TEMPERATURE_C = 60.0


def _check_temperature(device):
    for name, motor in device.motors.items():
        state = motor.get_state()
        values = () if state is None else (state.t_mos, state.t_rotor)
        temperatures = [
            value
            for value in values
            if not isinstance(value, bool)
            and isinstance(value, (int, float))
            and math.isfinite(value)
            and value > 0
        ]
        if not temperatures or max(temperatures) >= MAX_BENCH_TEMPERATURE_C:
            raise RuntimeError(f"B601 {name} temperature unknown or >= 60 C; bench refused")


def _hold_targets(measured, delta):
    targets = hold_action(measured, MODEL)
    if any(abs(targets[joint] - measured[joint]) > delta for joint in targets):
        raise ValueError("Zero-boundary correction exceeds the configured relative target")
    return targets


def _acquire_can(port: str, lock_path: Path):
    if sys.platform != "linux":
        raise RuntimeError("B601-RS requires Linux SocketCAN")
    import fcntl

    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    lease = os.fdopen(fd, "a")
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise RuntimeError("CAN ownership lock must be a regular file")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(
                "B601 shared CAN lock is held; arrange an exclusive handoff"
            ) from exc
        # Other controllers need not share our lock. Never open a competing CAN socket.
        receivers = Path("/proc/net/can")
        if not receivers.is_dir():
            raise RuntimeError("Cannot verify existing SocketCAN receivers")
        for path in receivers.glob("rcvlist_*"):
            for line in path.read_text().splitlines():
                fields = line.split()
                if len(fields) >= 6 and fields[0] in {port, "any"}:
                    raise RuntimeError(
                        f"CAN interface {port} already has a receiver; arrange a handoff"
                    )
        result = subprocess.run(
            ["ip", "-details", "-json", "link", "show", "dev", port],
            check=True,
            capture_output=True,
            text=True,
            timeout=3,
        )
        interface = json.loads(result.stdout)[0]
        data = interface["linkinfo"]["info_data"]
        if (
            interface["linkinfo"]["info_kind"] != "can"
            or "UP" not in interface["flags"]
            or data["state"] != "ERROR-ACTIVE"
            or data["bittiming"]["bitrate"] != 1_000_000
            or "LISTEN-ONLY" in str(data.get("ctrlmode", "")).upper()
        ):
            raise RuntimeError(
                "B601 requires an already-UP, healthy 1-Mbps normal-mode CAN interface"
            )
        return lease
    except BaseException:
        lease.close()
        raise


def _driver_types():
    try:
        from lerobot.robots.rebot_b601_follower import (
            RebotB601Follower,
            RebotB601FollowerRobotConfig,
        )
        from motorbridge import Mode
    except ImportError as exc:
        raise RuntimeError(
            "B601-RS needs the prepared LeRobot B601 checkout and MotorBridge 0.5.6. "
            "Reuse the Jetson's B601 interpreter; the SO .[hardware] extra is not a B601 driver."
        ) from exc
    return RebotB601Follower, RebotB601FollowerRobotConfig, Mode.MIT


def _guarded_device(base, config, mit_mode, session_active):
    class GuardedFollower(base):
        def __init__(self, selected):
            super().__init__(selected)
            self.seeded = False
            self.seed_targets = None
            self.check_dispatch = lambda *args: None
            self.prepare_action = lambda action, measured: action

        def _read_feedback(self):
            started = time.monotonic()
            states = {}
            for name, motor in self.motors.items():
                self.check_dispatch()
                if time.monotonic() - started >= FEEDBACK_TIMEOUT:
                    raise TimeoutError("B601 feedback acquisition exceeded 150 ms")
                position = motor.robstride_get_param_f32(0x7019, 10)
                velocity = motor.robstride_get_param_f32(0x701B, 10) if name == "gripper" else 0.0
                if not math.isfinite(position) or not math.isfinite(velocity):
                    raise ValueError("Non-finite B601 feedback")
                states[name] = SimpleNamespace(pos=position, vel=velocity)
            self.check_dispatch()
            if time.monotonic() - started >= FEEDBACK_TIMEOUT:
                raise TimeoutError("B601 feedback acquisition exceeded 150 ms")
            validate_observation(
                {
                    name + ".pos": self._motor_to_public_position(name, math.degrees(state.pos))
                    for name, state in states.items()
                },
                MODEL,
            )
            return states

        def _dispatch(self, action=None):
            if self.bus is None:
                raise RuntimeError("B601 motor bus is not connected")
            states = self._read_feedback()
            measured = {
                name + ".pos": self._motor_to_public_position(name, math.degrees(state.pos))
                for name, state in states.items()
            }
            targets = (
                _hold_targets(measured, self.config.max_relative_target)
                if action is None
                else self.prepare_action(action, measured)
            )
            targets = validate_action(targets, MODEL)
            if any(
                abs(targets[joint] - measured[joint]) > self.config.max_relative_target + 1e-9
                for joint in targets
            ):
                raise ValueError("B601 targets exceed the measured relative-target bound")
            # Reuse LeRobot's joint/gripper dispatcher without repeating feedback acquisition.
            return {
                joint: self._send_joint(
                    joint.removesuffix(".pos"), target, states[joint.removesuffix(".pos")]
                )
                for joint, target in targets.items()
            }

        def send_action(self, action):
            return self._dispatch(validate_action(action, MODEL))

        def send_hold(self):
            return self._dispatch()

        def configure(self):
            self._validate_control_config()
            if not session_active():
                raise ConnectionError("Session ended before B601 setup")
            self.bus.disable_all()
            for motor in self.motors.values():
                if not session_active():
                    raise ConnectionError("Session ended during B601 mode setup")
                motor.ensure_mode(mit_mode)
            deadline = time.monotonic() + FEEDBACK_TIMEOUT

            def startup_check(*args):
                if not session_active() or time.monotonic() >= deadline:
                    raise TimeoutError("B601 startup HOLD expired before motor activation")

            self.check_dispatch = startup_check
            try:
                self.seed_targets = self.send_hold()
                _check_temperature(self)
                self.seeded = True
                for motor in self.motors.values():
                    startup_check()
                    motor.enable()
            finally:
                self.check_dispatch = lambda *args: None

        def _send_joint(self, name, position_deg, state=None):
            if state is None:
                raise RuntimeError("B601 dispatch requires fresh measured joint feedback")
            low, high = self.config.joint_limits[name]
            target = max(low, min(high, position_deg))
            present = self._motor_to_public_position(name, math.degrees(state.pos))
            if abs(target - present) > self.config.max_relative_target + 1e-9:
                raise ValueError("B601 dispatch exceeds the measured relative-target bound")
            self.check_dispatch(name + ".pos", target)
            return super()._send_joint(name, target, state)

        def disconnect(self):
            # Native disconnect clears faults. Do not clear faults or restore torque caps here.
            errors = []
            for motor in self.motors.values():
                if self.config.disable_torque_on_disconnect:
                    try:
                        motor.disable()
                    except Exception as exc:
                        errors.append(exc)
                try:
                    motor.close()
                except Exception as exc:
                    errors.append(exc)
            if self.bus is not None:
                try:
                    self.bus.close()
                except Exception as exc:
                    errors.append(exc)
            self.bus, self.motors = None, {}
            self._reset_gripper_impedance_state()
            if errors:
                raise ExceptionGroup("B601 disconnect failed; torque state is unconfirmed", errors)

    return GuardedFollower(config)


class B601Arm:
    def __init__(
        self,
        port: str,
        arm_id: str,
        delta: float,
        exit_mode: str,
        lock_path: str,
        max_speed: float = 2.0,
        session_active=lambda: True,
    ):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,15}", port):
            raise ValueError("B601 port must be a Linux CAN interface name")
        if not math.isfinite(delta) or not 0 < delta <= 1:
            raise ValueError("B601 relative target must be in (0, 1] degrees")
        if not math.isfinite(max_speed) or not 0 < max_speed <= 5:
            raise ValueError("B601 speed must be in (0, 5] degrees/second")
        if exit_mode not in {"hold", "torque-off"}:
            raise ValueError("Invalid B601 exit policy")
        self.lock_path = Path(lock_path)
        if not self.lock_path.is_absolute():
            raise ValueError("B601 CAN ownership lock path must be absolute")
        self.lease = None
        self.delta = float(delta)
        self.max_speed = float(max_speed)
        self.last_command_at = None
        self.last_targets = None
        self.first_command = True
        self.session_active = session_active
        base, config_type, mode = _driver_types()
        config = config_type(
            port=port,
            id=arm_id,
            motor_family="rs",
            can_adapter="socketcan",
            cameras={},
            max_relative_target=self.delta,
            disable_torque_on_disconnect=exit_mode == "torque-off",
        )
        schema = arm_model(MODEL)
        expected_limits = {
            joint.removesuffix(".pos"): (low, high) for joint, low, high in schema.bounds
        }
        if config.joint_limits != expected_limits:
            raise ValueError("Installed B601 RS joint limits do not match the bridge schema")
        self.device = _guarded_device(base, config, mode, session_active)
        if self.device.action_features != dict.fromkeys(schema.joints, float):
            raise ValueError("Installed B601 driver has an incompatible joint schema")
        if set(self.device.profile.joint_directions.values()) != {-1.0}:
            raise ValueError("Installed B601 RS joint directions do not match the bridge schema")

    def connect(self):
        self.lease = _acquire_can(self.device.config.port, self.lock_path)
        self.device.connect(calibrate=False)
        self.last_targets = self.device.seed_targets.copy()
        self.last_command_at = time.monotonic()

    def get_observation(self):
        return validate_observation(self.device.get_observation(), MODEL)

    def send_action(self, action, *, deadline=None, session_active=lambda: True):
        action = validate_action(action, MODEL)
        deadline = min(
            float("inf") if deadline is None else deadline,
            time.monotonic() + FEEDBACK_TIMEOUT,
        )

        admitted = {}

        def check(joint=None, target=None):
            if not self.session_active() or not session_active() or time.monotonic() >= deadline:
                raise TimeoutError("B601 command expired or session ended before joint dispatch")
            if joint is not None:
                low, high = admitted[joint]
                if not low - 1e-9 <= target <= high + 1e-9:
                    raise ValueError(
                        "B601 SDK target fell outside the admitted rate/position envelope"
                    )

        def prepare(selected, measured):
            if self.first_command and any(abs(action[j] - measured[j]) > 1 for j in action):
                raise ValueError(
                    "First B601 command must align within 1 degree of the measured pose"
                )
            now = time.monotonic()
            step = self.max_speed * min(0.1, max(0, now - self.last_command_at))
            limited = {}
            for joint, minimum, maximum in arm_model(MODEL).bounds:
                low = max(minimum, measured[joint] - self.delta, self.last_targets[joint] - step)
                high = min(maximum, measured[joint] + self.delta, self.last_targets[joint] + step)
                if low > high:
                    raise ValueError("B601 tracking error leaves no safe rate/position target")
                admitted[joint] = (low, high)
                limited[joint] = max(low, min(high, selected[joint]))
            return limited

        self.device.check_dispatch = check
        self.device.prepare_action = prepare
        try:
            check()
            _check_temperature(self.device)
            result = self.device.send_action(action)
            _check_temperature(self.device)
            check()
            self.first_command = False
            self.last_targets = validate_action(result, MODEL)
            self.last_command_at = time.monotonic()
            return result
        finally:
            self.device.check_dispatch = lambda *args: None
            self.device.prepare_action = lambda selected, measured: selected

    def hold(self):
        if self.device.bus is None or not self.device.seeded:
            return
        deadline = time.monotonic() + FEEDBACK_TIMEOUT

        def check(*args):
            if time.monotonic() >= deadline:
                raise TimeoutError("B601 shutdown HOLD feedback expired")

        self.device.check_dispatch = check
        try:
            self.device.send_hold()
        finally:
            self.device.check_dispatch = lambda *args: None

    def disconnect(self):
        try:
            self.device.disconnect()
        finally:
            if self.lease is not None:
                self.lease.close()
                self.lease = None
