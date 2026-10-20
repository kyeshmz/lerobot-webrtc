import json

import pytest

from lerobot_webrtc.models import arm_model
from lerobot_webrtc.protocol import (
    FollowerInbox,
    LeaderInbox,
    hold_action,
    validate_action,
    validate_observation,
)

MODEL = "b601-rs"


def position():
    return dict(zip(arm_model(MODEL).joints, (10, -20, -30, 40, -50, 60, -200), strict=True))


def test_b601_round_trip_preserves_seven_degree_axes_and_angular_gripper():
    follower = FollowerInbox(MODEL, 0.3, simulated=False)
    leader = LeaderInbox(MODEL)
    permit = follower.issue_permit(10, position())
    leader.receive(permit)
    assert leader.position == position()
    assert leader.simulated is False
    message = leader.command(position())
    assert json.loads(message)["units"] == "degrees"
    assert follower.receive(message, 10.1)
    assert follower.take(10.2).action == position()
    assert not follower.receive(leader.command(position()), 10.3)


def test_b601_observations_ignore_reordering_without_renewing_old_feedback():
    follower = FollowerInbox(MODEL, 0.3)
    leader = LeaderInbox(MODEL)
    older = follower.issue_permit(10, position())
    newer_position = {**position(), "wrist_yaw.pos": -40}
    newer = follower.issue_permit(10.1, newer_position)
    leader.receive(newer)
    permit = leader.permit
    leader.receive(older)
    assert leader.position == newer_position
    assert leader.observation_sequence == 1
    assert leader.permit == permit


@pytest.mark.parametrize("model", ["so100", "so101"])
def test_so_and_b601_protocols_cannot_be_mixed(model):
    b601 = FollowerInbox(MODEL, 0.3)
    with pytest.raises(ValueError, match="Incompatible"):
        LeaderInbox(model).receive(b601.issue_permit(10, position()))
    with pytest.raises(ValueError, match="Incompatible"):
        LeaderInbox(MODEL).receive(FollowerInbox(model, 0.3).issue_permit(10))
    with pytest.raises(ValueError):
        validate_action(position(), model)


@pytest.mark.parametrize(
    ("joint", "value"),
    [
        ("gripper.pos", 25),
        ("shoulder_lift.pos", 1),
        ("elbow_flex.pos", -201),
        ("wrist_yaw.pos", 91),
        ("wrist_roll.pos", float("nan")),
        ("wrist_flex.pos", True),
    ],
)
def test_b601_rejects_values_outside_its_own_schema(joint, value):
    with pytest.raises(ValueError):
        validate_action({**position(), joint: value}, MODEL)


def test_b601_requires_wrist_yaw_and_explicit_simulation_identity():
    action = position()
    del action["wrist_yaw.pos"]
    with pytest.raises(ValueError):
        validate_action(action, MODEL)
    follower = FollowerInbox(MODEL, 0.3, simulated=True)
    leader = LeaderInbox(MODEL)
    first = follower.issue_permit(10, position())
    leader.receive(first)
    data = json.loads(follower.issue_permit(10.1, position()))
    del data["simulated"]
    with pytest.raises(ValueError, match="simulation"):
        leader.receive(json.dumps(data))
    data["simulated"] = False
    with pytest.raises(ValueError, match="changed"):
        leader.receive(json.dumps(data))


def test_unknown_model_is_rejected_instead_of_falling_back_to_so100():
    with pytest.raises(ValueError, match="Unsupported"):
        LeaderInbox("b601")


@pytest.mark.parametrize("value", [0.3, 0.305421487, 0.35])
def test_gripper_feedback_tolerance_does_not_widen_command_bounds(value):
    observed = {**position(), "gripper.pos": value}
    assert validate_observation(observed, MODEL)["gripper.pos"] == value
    assert hold_action(observed, MODEL)["gripper.pos"] == 0
    with pytest.raises(ValueError):
        validate_action(observed, MODEL)
    leader = LeaderInbox(MODEL)
    follower = FollowerInbox(MODEL, 0.3)
    leader.receive(follower.issue_permit(10, observed))
    assert leader.position["gripper.pos"] == value
    with pytest.raises(ValueError):
        leader.command(observed)


@pytest.mark.parametrize(
    ("joint", "value"),
    [
        ("gripper.pos", 0.350001),
        ("gripper.pos", -270.350001),
        ("shoulder_lift.pos", 0.350001),
        ("wrist_yaw.pos", 90.350001),
    ],
)
def test_feedback_tolerance_does_not_hide_other_limit_violations(joint, value):
    with pytest.raises(ValueError):
        validate_observation({**position(), joint: value}, MODEL)


@pytest.mark.parametrize("joint,low,high", arm_model(MODEL).bounds)
@pytest.mark.parametrize("side", ["lower", "upper"])
def test_all_b601_joint_feedback_margins_preserve_strict_commands(joint, low, high, side):
    value = low - 0.305 if side == "lower" else high + 0.305
    observed = {**position(), joint: value}
    assert validate_observation(observed, MODEL)[joint] == value
    legal = hold_action(observed, MODEL)
    assert legal[joint] == (low if side == "lower" else high)
    validate_action(legal, MODEL)
    with pytest.raises(ValueError):
        validate_action(observed, MODEL)
