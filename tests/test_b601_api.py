"""Installed B601 LeRobot methods, fake MotorBridge only. No physical I/O is allowed."""

import importlib
import math
from types import SimpleNamespace

import pytest

import lerobot_webrtc.b601 as b601
from lerobot_webrtc.protocol import hold_action

pytestmark = pytest.mark.b601_api


@pytest.fixture
def installed_b601(monkeypatch, tmp_path):
    import motorbridge

    driver = importlib.import_module("lerobot.robots.rebot_b601_follower.rebot_b601_follower")
    robot = importlib.import_module("lerobot.robots.robot")
    monkeypatch.setattr(robot, "HF_LEROBOT_CALIBRATION", tmp_path)
    events = []
    control = SimpleNamespace(clock=10.0, initial_gripper=0.0, initial_elbow=0.0)
    monkeypatch.setattr(b601, "time", SimpleNamespace(monotonic=lambda: control.clock))

    def forbid_controller(*args, **kwargs):
        pytest.fail("Installed-driver tests must never open a real MotorBridge controller")

    monkeypatch.setattr(motorbridge.Controller, "__init__", forbid_controller)

    class Motor:
        def __init__(self, identifier, model):
            self.identifier = identifier
            self.model = model
            initial = (
                control.initial_gripper
                if identifier == 7
                else control.initial_elbow
                if identifier == 3
                else 0.0
            )
            self.position = -math.radians(initial)
            self.enabled = False
            self.sent = []

        def ensure_mode(self, mode):
            events.append(("mode", self.identifier))

        def robstride_get_param_f32(self, register, timeout):
            assert register in {0x7019, 0x701B}
            events.append(("read", self.identifier, register))
            return self.position if register == 0x7019 else 0.0

        def send_mit(self, position, velocity, kp, kd, torque):
            self.sent.append((position, velocity, kp, kd, torque))
            events.append(("send", self.identifier))
            if self.enabled and self.identifier != 7:
                self.position = position

        def enable(self):
            events.append(("enable", self.identifier))
            self.enabled = True

        def get_state(self):
            return SimpleNamespace(t_mos=25.0, t_rotor=0)

        def disable(self):
            events.append(("disable", self.identifier))
            self.enabled = False

        def clear_error(self):
            pytest.fail("B601 bridge must never clear motor faults")

        def set_zero_position(self):
            pytest.fail("B601 bridge must never zero a motor")

        def close(self):
            events.append(("close", self.identifier))

    class Controller:
        def __init__(self, channel):
            assert channel == "can1"
            self.motors = []

        def add_robstride_motor(self, identifier, host, model):
            assert host == 0xFD
            motor = Motor(identifier, model)
            self.motors.append(motor)
            return motor

        def disable_all(self):
            events.append(("disable-all",))
            for motor in self.motors:
                motor.enabled = False

        def enable_all(self):
            pytest.fail("Use measured HOLD and per-motor admission, not native enable_all")

        def close(self):
            events.append(("bus-close",))

    monkeypatch.setattr(driver, "MotorBridgeController", Controller)
    monkeypatch.setattr(
        b601,
        "_acquire_can",
        lambda *_: SimpleNamespace(close=lambda: events.append(("lease-close",))),
    )

    def make(exit_mode="hold"):
        return b601.B601Arm("can1", "test_b601", 0.5, exit_mode, str(tmp_path / "can.lock"))

    return SimpleNamespace(make=make, events=events, control=control)


@pytest.mark.parametrize("exit_mode", ["hold", "torque-off"])
def test_installed_b601_lifecycle_and_real_degree_action_methods(installed_b601, exit_mode):
    arm = installed_b601.make(exit_mode)
    try:
        arm.connect()
        first_enable = next(i for i, e in enumerate(installed_b601.events) if e[0] == "enable")
        assert len([e for e in installed_b601.events[:first_enable] if e[0] == "send"]) == 7
        assert [motor.model for motor in arm.device.motors.values()] == ["rs-06"] * 3 + [
            "rs-00"
        ] * 4
        origin = arm.get_observation()
        assert len(origin) == 7 and origin["wrist_yaw.pos"] == 0
        installed_b601.control.clock += 0.05
        values = (0.1, -0.1, -0.1, 0.1, -0.1, 0.1, -0.1)
        target = dict(zip(origin, values, strict=True))
        sent = arm.send_action(target, deadline=installed_b601.control.clock + 0.1)
        assert sent == pytest.approx(target)
        for joint in tuple(origin)[:-1]:
            motor = arm.device.motors[joint.removesuffix(".pos")]
            assert motor.sent[-1][0] == pytest.approx(-math.radians(target[joint]))
        gripper = arm.device.motors["gripper"]
        assert 0 < gripper.sent[-1][-1] <= 1.0
        observed = arm.get_observation()
        for joint in tuple(origin)[:-1]:
            assert observed[joint] == pytest.approx(target[joint])
        arm.hold()
    finally:
        arm.disconnect()
    assert not arm.device.is_connected
    assert len([e for e in installed_b601.events if e[0] == "disable"]) == (
        7 if exit_mode == "torque-off" else 0
    )
    assert installed_b601.events[-1] == ("lease-close",)


def test_installed_driver_accepts_small_feedback_overshoot_but_never_commands_it(installed_b601):
    installed_b601.control.initial_gripper = 0.305421487
    installed_b601.control.initial_elbow = 0.005
    arm = installed_b601.make()
    try:
        arm.connect()
        assert arm.get_observation()["gripper.pos"] == pytest.approx(0.305421487)
        assert arm.device.seed_targets["gripper.pos"] == 0
        assert arm.device.seed_targets["elbow_flex.pos"] == 0
        installed_b601.control.clock += 0.05
        action = {**hold_action(arm.get_observation(), "b601-rs"), "shoulder_pan.pos": 0.1}
        sent = arm.send_action(action)
        assert sent["gripper.pos"] == 0
        assert sent["shoulder_pan.pos"] == pytest.approx(0.1)
        with pytest.raises(ValueError):
            arm.send_action({**action, "gripper.pos": 0.3})
        arm.hold()
    finally:
        arm.disconnect()


def test_installed_dispatch_and_hold_do_not_duplicate_feedback_acquisition(installed_b601):
    arm = installed_b601.make()
    try:
        arm.connect()
        action = hold_action(arm.get_observation(), "b601-rs")
        installed_b601.events.clear()
        installed_b601.control.clock += 0.05
        arm.send_action(action)
        assert len([event for event in installed_b601.events if event[0] == "read"]) == 8
        assert len([event for event in installed_b601.events if event[0] == "send"]) == 7
        installed_b601.events.clear()
        arm.hold()
        assert len([event for event in installed_b601.events if event[0] == "read"]) == 8
        assert len([event for event in installed_b601.events if event[0] == "send"]) == 7
    finally:
        arm.disconnect()
