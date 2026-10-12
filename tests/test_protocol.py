import json

import pytest

from lerobot_webrtc.protocol import JOINTS, FollowerInbox, LeaderInbox, validate_action


def action(value=0.0):
    return {joint: value for joint in JOINTS}


def pair(now=10.0):
    follower = FollowerInbox("so101", 0.3)
    leader = LeaderInbox("so101")
    leader.receive(follower.issue_permit(now))
    return leader, follower


def test_round_trip():
    leader, follower = pair()
    assert follower.receive(leader.command(action(10)), 10.05)
    command = follower.take(10.06)
    assert command.action == action(10)
    assert command.deadline == 10.3
    assert follower.take(10.07) is None


def test_latest_only_and_out_of_order():
    leader, follower = pair()
    older, newer = leader.command(action(1)), leader.command(action(2))
    assert follower.receive(newer, 10.01)
    assert not follower.receive(older, 10.02)
    assert not follower.receive(newer, 10.03)
    assert follower.take(10.04).action == action(2)


def test_stale_or_forged_permit_cannot_move_arm():
    leader, follower = pair()
    packet = leader.command(action())
    assert not follower.receive(packet, 10.3)
    data = json.loads(packet)
    data["permit"] = "a" * 32
    assert not follower.receive(json.dumps(data), 10.1)
    assert follower.take(10.1) is None


def test_pending_command_expires_before_application():
    leader, follower = pair()
    follower.receive(leader.command(action()), 10.1)
    assert follower.take(10.31) is None


def test_model_and_units_must_match():
    leader, follower = pair()
    data = json.loads(leader.command(action()))
    for key, value in (("model", "so100"), ("units", "degrees"), ("v", True)):
        invalid = {**data, key: value}
        with pytest.raises(ValueError):
            follower.receive(json.dumps(invalid), 10.01)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -101, 101, True, "1", None])
def test_bad_positions(value):
    positions = action()
    positions["elbow_flex.pos"] = value
    with pytest.raises(ValueError):
        validate_action(positions)


def test_wrong_joint_set_and_gripper_range():
    with pytest.raises(ValueError):
        validate_action({"shoulder_pan.pos": 0})
    with pytest.raises(ValueError):
        validate_action({**action(), "unexpected.pos": 0})
    with pytest.raises(ValueError):
        validate_action({**action(), "gripper.pos": -1})


@pytest.mark.parametrize("message", ["[]", "null", "{", b"{}", " " * 5000])
def test_malformed_packets(message):
    _, follower = pair()
    with pytest.raises(ValueError):
        follower.receive(message, 10.01)


@pytest.mark.parametrize("seq", [-1, True, 1.5, "0", 2**53])
def test_invalid_sequence(seq):
    leader, follower = pair()
    data = json.loads(leader.command(action()))
    data["seq"] = seq
    with pytest.raises(ValueError):
        follower.receive(json.dumps(data), 10.01)


def test_permits_are_pruned():
    _, follower = pair()
    follower.issue_permit(11)
    assert len(follower.permits) == 1


def test_sample_keeps_original_permit():
    leader, follower = pair()
    original = leader.permit
    leader.receive(follower.issue_permit(10.4))
    delayed_sample = leader.command(action(), original)
    assert not follower.receive(delayed_sample, 10.41)
