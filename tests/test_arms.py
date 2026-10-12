"""Test the LeRobot integration boundary without installing ML dependencies."""

import sys
from types import ModuleType, SimpleNamespace

import pytest

from lerobot_webrtc.arms import LeRobotArm
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
            self.bus = SimpleNamespace(is_connected=False)
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
