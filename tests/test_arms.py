"""Test the LeRobot integration boundary without installing ML dependencies."""

import asyncio
import sys
import threading
import time
from types import ModuleType, SimpleNamespace

import pytest

from lerobot_webrtc.arms import AsyncArm, LeRobotArm, SendResult, SimulatedArm
from lerobot_webrtc.b601 import B601Arm
from lerobot_webrtc.protocol import JOINTS


@pytest.fixture
def fake_lerobot(monkeypatch):
    class Config:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class Device:
        def __init__(self, config):
            self.config = config
            self.calibration = {"saved": True}
            self.is_calibrated = True
            self.bus = SimpleNamespace(
                is_connected=False,
                sync_read=lambda register, **kwargs: dict.fromkeys(
                    (joint.removesuffix(".pos") for joint in JOINTS), 10.0
                ),
            )
            self.calls = []

        @property
        def is_connected(self):
            return self.bus.is_connected

        def connect(self, calibrate):
            self.bus.is_connected = True
            self.calls.append(("connect", calibrate))

        def get_action(self):
            return dict.fromkeys(JOINTS, 0.0)

        def get_observation(self):
            return {**dict.fromkeys(JOINTS, 10.0), "camera": "must-not-be-a-command"}

        def send_action(self, action):
            self.calls.append(("send", action))
            return action

        def calibrate(self):
            self.calls.append(("calibrate",))

        def disconnect(self):
            self.calls.append(("disconnect",))
            self.bus.is_connected = False

    for name in ("lerobot", "lerobot.robots", "lerobot.teleoperators"):
        monkeypatch.setitem(sys.modules, name, ModuleType(name))
    for parent, suffix, module_name in (
        ("lerobot.teleoperators", "Leader", "so_leader"),
        ("lerobot.robots", "Follower", "so_follower"),
    ):
        module = ModuleType(f"{parent}.{module_name}")
        for model in ("SO100", "SO101"):
            setattr(module, model + suffix, Device)
            setattr(module, model + suffix + "Config", Config)
        setattr(sys.modules[parent], module_name, module)


@pytest.mark.parametrize("model", ["so100", "so101"])
@pytest.mark.parametrize("role", ["leader", "follower"])
def test_reuses_lerobot_config_and_api(fake_lerobot, model, role):
    arm = LeRobotArm(role, model, "/dev/example", "calibration-id", 3, "hold")
    config = arm.device.config
    assert config.port == "/dev/example"
    assert config.id == "calibration-id"
    assert config.use_degrees is False
    if role == "follower":
        assert config.max_relative_target == 3
        assert config.disable_torque_on_disconnect is False
    arm.connect()
    assert arm.device.calls == [("connect", False)]
    if role == "leader":
        assert arm.get_action() == dict.fromkeys(JOINTS, 0.0)
    else:
        arm.hold()
        assert arm.device.calls[-1] == ("send", dict.fromkeys(JOINTS, 10.0))
    arm.disconnect()
    assert not arm.device.bus.is_connected


def test_missing_calibration_refuses_to_open_bus(fake_lerobot):
    arm = LeRobotArm("follower", "so101", "/dev/example", "id", 5, "hold")
    arm.device.calibration = {}
    with pytest.raises(RuntimeError, match="No saved calibration"):
        arm.connect()
    assert not arm.device.bus.is_connected
    arm.hold()
    assert arm.device.calls == []


def test_mismatched_calibration_refuses_motion_but_can_disconnect(fake_lerobot):
    arm = LeRobotArm("follower", "so101", "/dev/example", "id", 5, "hold")
    arm.device.is_calibrated = False
    with pytest.raises(RuntimeError, match="calibration mismatch"):
        arm.connect()
    arm.hold()
    assert not any(call[0] == "send" for call in arm.device.calls)
    arm.disconnect()
    assert not arm.device.bus.is_connected


def test_torque_off_is_explicit(fake_lerobot):
    arm = LeRobotArm("follower", "so101", "/dev/example", "id", 5, "torque-off")
    assert arm.device.config.disable_torque_on_disconnect is True


def test_measured_joint_observation_never_reads_sdk_cameras(fake_lerobot):
    arm = LeRobotArm("follower", "so101", "/dev/example", "id", 5, "hold")
    arm.connect()
    arm.device.get_observation = lambda: pytest.fail("SDK camera read on the motor worker")
    reads = []

    arm.device.config.num_read_retries = 2

    def read(register, num_retry):
        reads.append((register, num_retry))
        return dict.fromkeys((joint.removesuffix(".pos") for joint in JOINTS), 7.0)

    arm.device.bus.sync_read = read
    assert arm.model == "so101"
    assert arm.get_observation() == dict.fromkeys(JOINTS, 7.0)
    arm.hold()
    assert reads == [("Present_Position", 2), ("Present_Position", 2)]
    assert arm.device.calls[-1] == ("send", dict.fromkeys(JOINTS, 7.0))


def test_measured_joint_observation_validates_bus_feedback(fake_lerobot):
    arm = LeRobotArm("follower", "so101", "/dev/example", "id", 5, "hold")
    arm.device.bus.sync_read = lambda register, **kwargs: dict.fromkeys(
        (joint.removesuffix(".pos") for joint in JOINTS), float("nan")
    )
    with pytest.raises(ValueError, match="Out-of-range"):
        arm.get_observation()


async def test_result_preserves_driver_clamping_not_requested_action():
    device = SimulatedArm(delta=2)
    arm = AsyncArm(device, "follower")
    requested = dict.fromkeys(JOINTS, 20.0)
    try:
        result = await arm.send_with_result(requested, time.monotonic() + 1)
        assert result == SendResult(True, dict.fromkeys(JOINTS, 2.0))
        assert result.action is not requested
        assert requested == dict.fromkeys(JOINTS, 20.0)
    finally:
        await arm.close()


async def test_none_driver_return_is_still_accepted_by_legacy_send():
    class NoResultArm(SimulatedArm):
        def send_action(self, action):
            super().send_action(action)

    device = NoResultArm()
    arm = AsyncArm(device, "follower")
    try:
        assert await arm.send(device.position, time.monotonic() + 1) is True
        assert await arm.send_with_result(device.position, time.monotonic() + 1) == SendResult(True)
    finally:
        await arm.close()


@pytest.mark.parametrize("revocation", ["deadline", "session", "close", "cancel"])
async def test_result_write_rechecks_admission_after_waiting_for_worker(revocation):
    class BlockingArm(SimulatedArm):
        def block(self):
            started.set()
            release.wait()

    started, release = threading.Event(), threading.Event()
    device = BlockingArm()
    arm = AsyncArm(device, "follower")
    active = True
    close = None
    blocker = asyncio.create_task(arm.call("block"))
    try:
        assert await asyncio.to_thread(started.wait, 1)
        send = asyncio.create_task(
            arm.send_with_result(
                device.position,
                time.monotonic() + (0.02 if revocation == "deadline" else 1),
                session_active=lambda: active,
            )
        )
        await asyncio.sleep(0.03)
        if revocation == "session":
            active = False
        elif revocation == "close":
            close = asyncio.create_task(arm.close())
            await asyncio.sleep(0)
        elif revocation == "cancel":
            send.cancel()
            await asyncio.sleep(0)
        release.set()
        await blocker
        if revocation == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await send
        else:
            assert await send == SendResult(False)
        assert device.applied == 0
        if close is not None:
            await close
    finally:
        release.set()
        await arm.close()
    assert await arm.send_with_result(device.position, time.monotonic() + 1) == SendResult(False)


async def test_result_cancellation_joins_running_write_even_when_repeated():
    class BlockingArm(SimulatedArm):
        def send_action(self, action):
            started.set()
            release.wait()
            return super().send_action(action)

    started, release = threading.Event(), threading.Event()
    device = BlockingArm()
    arm = AsyncArm(device, "follower")
    send = asyncio.create_task(arm.send_with_result(device.position, time.monotonic() + 1))
    try:
        assert await asyncio.to_thread(started.wait, 1)
        send.cancel()
        await asyncio.sleep(0)
        send.cancel()
        await asyncio.sleep(0)
        assert not send.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await send
        assert device.applied == 1
    finally:
        release.set()
        await arm.close()


async def test_result_boundary_forwards_b601_per_joint_guards():
    class GuardedB601(B601Arm):
        def __init__(self):
            self.seen = None

        def send_action(self, action, *, deadline, session_active):
            self.seen = deadline, session_active
            assert session_active()
            return action

        def hold(self):
            pass

        def disconnect(self):
            pass

    device = GuardedB601()
    arm = AsyncArm(device, "follower")
    deadline = time.monotonic() + 1

    def active():
        return True

    try:
        result = await arm.send_with_result({"driver": "result"}, deadline, active)
        assert result == SendResult(True, {"driver": "result"})
        assert device.seen == (deadline, active)
    finally:
        await arm.close()


@pytest.mark.hardware_api
async def test_installed_so_driver_joint_only_feedback_and_actual_clamped_return(
    monkeypatch,
    tmp_path,
):
    import importlib

    import serial
    from lerobot.motors import MotorCalibration

    from .test_lerobot_api import MemoryMotorBus

    def forbid_serial(*args, **kwargs):
        pytest.fail("Native arm API test must never open a serial port")

    monkeypatch.setattr(serial.Serial, "open", forbid_serial)
    module = importlib.import_module("lerobot.robots.so_follower.so_follower")
    monkeypatch.setattr(module, "FeetechMotorsBus", MemoryMotorBus)
    robot = importlib.import_module("lerobot.robots.robot")
    monkeypatch.setattr(robot, "HF_LEROBOT_CALIBRATION", tmp_path)
    device = LeRobotArm("follower", "so101", "/not-a-port", "native-api", 3, "hold")
    device.device.calibration = {
        name: MotorCalibration(
            id=motor.id,
            drive_mode=0,
            homing_offset=0,
            range_min=0,
            range_max=4095,
        )
        for name, motor in device.device.bus.motors.items()
    }
    device.device.bus.calibration = device.device.calibration
    arm = AsyncArm(device, "follower")
    try:
        await arm.call("connect")
        device.device.cameras["never-read"] = SimpleNamespace(
            is_connected=True,
            read_latest=lambda: pytest.fail("SDK camera must not run on the motor worker"),
            disconnect=lambda: None,
        )
        measured = await arm.call("get_observation")
        assert measured == dict.fromkeys(JOINTS, 0.0)
        result = await arm.send_with_result(dict.fromkeys(JOINTS, 20.0), time.monotonic() + 1)
        assert result == SendResult(True, dict.fromkeys(JOINTS, 3.0))
        assert result.action != measured
        assert await arm.call("get_observation") == result.action
    finally:
        await arm.close()
