import math
from types import SimpleNamespace

import pytest

import lerobot_webrtc.b601 as b601
from lerobot_webrtc.arms import LeRobotArm
from lerobot_webrtc.models import arm_model


@pytest.fixture
def fake_b601(monkeypatch, tmp_path):
    events = []
    schema = arm_model("b601-rs")
    control = SimpleNamespace(
        clock=10.0,
        after_write=lambda name: None,
        close_failure=None,
        temperature=25.0,
        initial_gripper=0.0,
        jammed=False,
    )
    monkeypatch.setattr(b601, "time", SimpleNamespace(monotonic=lambda: control.clock))

    class Motor:
        def __init__(self, name):
            self.name = name
            self.position = -math.radians(control.initial_gripper) if name == "gripper" else 0.0

        def robstride_get_param_f32(self, register, timeout):
            events.append(("read", self.name, register))
            return self.position if register == 0x7019 else 0.0

        def ensure_mode(self, mode):
            events.append(("mode", self.name, mode))

        def enable(self):
            events.append(("enable", self.name))

        def get_state(self):
            return SimpleNamespace(t_mos=control.temperature, t_rotor=0)

        def disable(self):
            events.append(("disable", self.name))

        def close(self):
            events.append(("close", self.name))
            if self.name == control.close_failure:
                raise RuntimeError("handle close failed")

    class Bus:
        def disable_all(self):
            events.append(("disable-all",))

        def close(self):
            events.append(("bus-close",))

    class Config:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)
            self.joint_limits = {
                name.removesuffix(".pos"): (low, high) for name, low, high in schema.bounds
            }

    class Driver:
        def __init__(self, config):
            self.config = config
            self.bus = None
            self.motors = {}
            self.profile = SimpleNamespace(
                joint_directions=dict.fromkeys(config.joint_limits, -1.0)
            )
            self.action_features = dict.fromkeys(schema.joints, float)

        def connect(self, calibrate):
            assert calibrate is False
            events.append(("connect",))
            self.bus = Bus()
            self.motors = {name: Motor(name) for name in self.config.joint_limits}
            self.configure()

        def _validate_control_config(self):
            pass

        def _motor_to_public_position(self, name, degrees):
            return -degrees

        def get_observation(self):
            return {
                name + ".pos": -math.degrees(s.pos) for name, s in self._read_feedback().items()
            }

        def send_action(self, action):
            states = self._read_feedback()
            return {
                name: self._send_joint(
                    name.removesuffix(".pos"), value, states[name.removesuffix(".pos")]
                )
                for name, value in action.items()
            }

        def _send_joint(self, name, value, state=None):
            events.append(("send", name, value))
            if not control.jammed:
                self.motors[name].position = -math.radians(value)
            control.after_write(name)
            return value

        def _reset_gripper_impedance_state(self):
            events.append(("reset-filter",))

    lease = SimpleNamespace(close=lambda: events.append(("lease-close",)))
    monkeypatch.setattr(b601, "_driver_types", lambda: (Driver, Config, "MIT"))
    monkeypatch.setattr(b601, "_acquire_can", lambda *_: lease)

    def make(exit_mode="hold", delta=0.5):
        return b601.B601Arm("can1", "test", delta, exit_mode, str(tmp_path / "can.lock"))

    return SimpleNamespace(make=make, events=events, control=control)


def test_startup_seeds_all_seven_measured_targets_before_any_enable(fake_b601):
    arm = fake_b601.make()
    arm.connect()
    first_enable = next(i for i, event in enumerate(fake_b601.events) if event[0] == "enable")
    seeds = [e for e in fake_b601.events[:first_enable] if e[0] == "send"]
    assert len(seeds) == 7
    assert all(e[2] == 0 for e in seeds)
    assert arm.device.config.motor_family == "rs"
    assert arm.device.config.can_adapter == "socketcan"
    arm.hold()
    arm.disconnect()
    assert not [e for e in fake_b601.events if e[0] == "disable"]
    assert fake_b601.events[-1] == ("lease-close",)


def test_first_command_alignment_and_step_rate_are_enforced(fake_b601):
    arm = fake_b601.make()
    arm.connect()
    origin = arm.get_observation()
    with pytest.raises(ValueError, match="align"):
        arm.send_action({**origin, "shoulder_pan.pos": 2})
    fake_b601.control.clock += 0.05
    result = arm.send_action({**origin, "shoulder_pan.pos": 0.5})
    assert result["shoulder_pan.pos"] == pytest.approx(0.1)
    arm.disconnect()


def test_small_gripper_zero_overshoot_seeds_only_legal_targets(fake_b601):
    fake_b601.control.initial_gripper = 0.305421487
    arm = fake_b601.make()
    arm.connect()
    assert arm.device.seed_targets["gripper.pos"] == 0
    assert all(event[2] <= 0 for event in fake_b601.events if event[:2] == ("send", "gripper"))
    with pytest.raises(ValueError):
        arm.send_action({**arm.get_observation(), "gripper.pos": 0.3})
    arm.disconnect()


def test_target_rate_accumulates_but_never_exceeds_measured_relative_limit(fake_b601):
    fake_b601.control.jammed = True
    arm = fake_b601.make()
    arm.connect()
    target = {**arm.get_observation(), "shoulder_pan.pos": 0.5}
    for step in range(1, 8):
        fake_b601.control.clock += 0.05
        result = arm.send_action(target)
        assert result["shoulder_pan.pos"] == pytest.approx(min(0.1 * step, 0.5))
    arm.disconnect()


def test_command_and_hold_each_use_exactly_one_fresh_acquisition(fake_b601):
    arm = fake_b601.make()
    arm.connect()
    target = arm.get_observation()
    fake_b601.events.clear()
    fake_b601.control.clock += 0.05
    arm.send_action(target)
    assert len([event for event in fake_b601.events if event[0] == "read"]) == 8
    assert len([event for event in fake_b601.events if event[0] == "send"]) == 7
    fake_b601.events.clear()
    arm.hold()
    assert len([event for event in fake_b601.events if event[0] == "read"]) == 8
    assert len([event for event in fake_b601.events if event[0] == "send"]) == 7
    arm.disconnect()


def test_zero_tolerance_never_bypasses_a_smaller_relative_target_limit(fake_b601):
    fake_b601.control.initial_gripper = 0.305421487
    arm = fake_b601.make(delta=0.1)
    with pytest.raises(ValueError, match="correction exceeds"):
        arm.connect()
    assert not [event for event in fake_b601.events if event[0] == "enable"]
    arm.disconnect()


def test_original_deadline_is_rechecked_between_joint_writes(fake_b601):
    arm = fake_b601.make()
    arm.connect()
    action = arm.get_observation()
    fake_b601.events.clear()
    fake_b601.control.clock += 0.05
    deadline = fake_b601.control.clock + 0.02
    fake_b601.control.after_write = lambda _: setattr(fake_b601.control, "clock", deadline + 0.001)
    with pytest.raises(TimeoutError, match="expired"):
        arm.send_action(action, deadline=deadline)
    assert [e[1] for e in fake_b601.events if e[0] == "send"] == ["shoulder_pan"]
    fake_b601.control.after_write = lambda _: None
    arm.disconnect()


def test_zero_deadline_never_dispatches(fake_b601):
    arm = fake_b601.make()
    arm.connect()
    action = arm.get_observation()
    fake_b601.events.clear()
    with pytest.raises(TimeoutError):
        arm.send_action(action, deadline=0)
    assert not [e for e in fake_b601.events if e[0] == "send"]
    arm.disconnect()


def test_disconnect_attempts_every_motor_and_releases_lease_on_failure(fake_b601):
    arm = fake_b601.make("torque-off")
    arm.connect()
    fake_b601.control.close_failure = "shoulder_pan"
    with pytest.raises(ExceptionGroup, match="unconfirmed"):
        arm.disconnect()
    assert len([e for e in fake_b601.events if e[0] == "disable"]) == 7
    assert len([e for e in fake_b601.events if e[0] == "close"]) == 7
    assert ("bus-close",) in fake_b601.events
    assert fake_b601.events[-1] == ("lease-close",)


def test_b601_cannot_accidentally_construct_an_so_driver():
    with pytest.raises(ValueError, match="B601Arm"):
        LeRobotArm("follower", "b601-rs", "can1", "b601", 0.5, "hold")


@pytest.mark.parametrize("temperature", [0, 60, 90, float("nan"), None])
def test_unknown_or_hot_motor_temperature_blocks_enable(fake_b601, temperature):
    fake_b601.control.temperature = temperature
    arm = fake_b601.make()
    with pytest.raises(RuntimeError, match="temperature"):
        arm.connect()
    assert not [event for event in fake_b601.events if event[0] == "enable"]
    arm.disconnect()


def test_busy_can_refuses_before_any_interface_command(monkeypatch, tmp_path):
    monkeypatch.setattr(b601, "sys", SimpleNamespace(platform="linux"))
    proc = tmp_path / "proc"
    proc.mkdir()
    (proc / "rcvlist_all").write_text("can1 000 00000000 00000000 00000000 10 raw\n")
    monkeypatch.setattr(b601, "Path", lambda _: proc)

    def forbidden(*args, **kwargs):
        pytest.fail("Busy CAN must be rejected before any interface command")

    monkeypatch.setattr(b601.subprocess, "run", forbidden)
    with pytest.raises(RuntimeError, match="handoff"):
        b601._acquire_can("can1", tmp_path / "can.lock")
