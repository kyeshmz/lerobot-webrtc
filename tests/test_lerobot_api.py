"""Run installed LeRobot 0.6.1 drivers; replace only the motor bus, never open serial."""

import asyncio
import importlib
from contextlib import contextmanager
from importlib.metadata import version

import pytest

from lerobot_webrtc.arms import AsyncArm, LeRobotArm
from lerobot_webrtc.peer import Peer
from lerobot_webrtc.protocol import JOINTS
from lerobot_webrtc.runtime import run_control

from .conftest import TOKEN

pytestmark = pytest.mark.hardware_api


class MemoryMotorBus:
    def __init__(self, port, motors, calibration):
        self.port = port
        self.motors = motors
        self.calibration = calibration
        self.is_connected = False
        self.position = dict.fromkeys(motors, 0.0)
        self.writes = []
        self.disable_torque_on_disconnect = None

    @property
    def is_calibrated(self):
        return bool(self.calibration)

    def connect(self):
        self.is_connected = True

    def disconnect(self, disable_torque=True):
        self.disable_torque_on_disconnect = disable_torque
        self.is_connected = False

    def configure_motors(self):
        pass

    def disable_torque(self):
        pass

    @contextmanager
    def torque_disabled(self):
        yield

    def write(self, register, motor, value):
        pass

    def sync_read(self, register, num_retry):
        assert self.is_connected
        assert register == "Present_Position"
        return self.position.copy()

    def sync_write(self, register, values):
        assert self.is_connected
        assert register == "Goal_Position"
        self.writes.append(values.copy())
        self.position.update(values)


@pytest.fixture
def installed_lerobot(monkeypatch, tmp_path):
    import serial
    from lerobot.motors import MotorCalibration

    assert version("lerobot") == "0.6.1"

    def forbid_serial(*args, **kwargs):
        pytest.fail("Driver API tests must never open a physical serial port")

    monkeypatch.setattr(serial.Serial, "open", forbid_serial)
    for module_name in (
        "lerobot.robots.so_follower.so_follower",
        "lerobot.teleoperators.so_leader.so_leader",
    ):
        module = importlib.import_module(module_name)
        monkeypatch.setattr(module, "FeetechMotorsBus", MemoryMotorBus)
    for module_name in ("lerobot.robots.robot", "lerobot.teleoperators.teleoperator"):
        module = importlib.import_module(module_name)
        monkeypatch.setattr(module, "HF_LEROBOT_CALIBRATION", tmp_path)

    def make_arm(role, model, delta=3, exit_mode="hold"):
        args = (role, model, "/not-a-serial-port", f"verify_{model}_{role}", delta, exit_mode)
        initial = LeRobotArm(*args)
        initial.device.calibration = {
            name: MotorCalibration(
                id=motor.id, drive_mode=0, homing_offset=0, range_min=0, range_max=4095
            )
            for name, motor in initial.device.bus.motors.items()
        }
        initial.device._save_calibration()
        return LeRobotArm(*args)

    return make_arm


@pytest.mark.parametrize("model", ["so100", "so101"])
@pytest.mark.parametrize("role", ["leader", "follower"])
def test_actual_driver_config_joint_schema_and_calibration(installed_lerobot, model, role):
    from lerobot.motors import MotorNormMode

    arm = installed_lerobot(role, model)
    device = arm.device
    assert device.action_features == dict.fromkeys(JOINTS, float)
    assert device.calibration == device.bus.calibration
    assert device.config.use_degrees is False
    assert [motor.id for motor in device.bus.motors.values()] == [1, 2, 3, 4, 5, 6]
    for name, motor in device.bus.motors.items():
        expected = MotorNormMode.RANGE_0_100 if name == "gripper" else MotorNormMode.RANGE_M100_100
        assert motor.norm_mode == expected
    try:
        arm.connect()
        assert device.is_connected
        if role == "leader":
            assert arm.get_action() == dict.fromkeys(JOINTS, 0.0)
        else:
            assert device.config.max_relative_target == 3
            assert isinstance(device.config.max_relative_target, float)
            arm.hold()
            assert device.bus.writes == [dict.fromkeys(device.bus.motors, 0.0)]
    finally:
        arm.disconnect()
    assert not device.is_connected


@pytest.mark.parametrize("exit_mode", ["hold", "torque-off"])
def test_actual_follower_limiter_and_disconnect_policy(installed_lerobot, exit_mode):
    arm = installed_lerobot("follower", "so101", delta=3, exit_mode=exit_mode)
    try:
        arm.connect()
        target = dict(zip(JOINTS, (9, -11, 13, -17, 19, 23), strict=True))
        result = arm.send_action(target)
        expected = {joint: 3.0 if value > 0 else -3.0 for joint, value in target.items()}
        assert result == expected
        assert arm.device.bus.writes[-1] == {
            joint.removesuffix(".pos"): value for joint, value in expected.items()
        }
        arm.hold()
        assert arm.device.bus.writes[-1] == arm.device.bus.writes[-2]
    finally:
        arm.disconnect()
    assert arm.device.bus.disable_torque_on_disconnect is (exit_mode == "torque-off")


@pytest.mark.parametrize("model", ["so100", "so101"])
async def test_actual_drivers_control_all_joints_over_webrtc(
    installed_lerobot, signaling_server, model
):
    url, _ = signaling_server
    leader = installed_lerobot("leader", model)
    follower = installed_lerobot("follower", model)
    leader_peer = Peer("leader", url, "driver-api", TOKEN)
    follower_peer = Peer("follower", url, "driver-api", TOKEN)
    tasks = [
        asyncio.create_task(
            run_control(AsyncArm(leader, "leader"), leader_peer, model, 30, 0.3, None)
        ),
        asyncio.create_task(
            run_control(AsyncArm(follower, "follower"), follower_peer, model, 30, 0.3, None)
        ),
    ]
    try:
        for values in ((9, -11, 13, -17, 19, 23), (-7, 5, -3, 11, -13, 3)):
            target = dict(zip(leader.device.bus.motors, values, strict=True))
            leader.device.bus.position = target
            async with asyncio.timeout(10):
                while follower.device.bus.position != target:
                    for task in tasks:
                        if task.done():
                            task.result()
                            pytest.fail("Driver endpoint exited before reaching its target")
                    await asyncio.sleep(0.01)
        previous = dict.fromkeys(follower.device.bus.motors, 0.0)
        for current in follower.device.bus.writes:
            assert all(abs(current[joint] - previous[joint]) <= 3 for joint in current)
            previous = current
        writes_before_shutdown = len(follower.device.bus.writes)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    assert not leader.device.is_connected
    assert not follower.device.is_connected
    assert follower.device.bus.disable_torque_on_disconnect is False
    assert len(follower.device.bus.writes) >= writes_before_shutdown + 1
    assert follower.device.bus.writes[-1] == follower.device.bus.position
