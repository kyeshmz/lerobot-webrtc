"""Acceptance checks over real WebSocket signaling and encrypted WebRTC, without motors."""

import asyncio
from types import SimpleNamespace

import pytest
import pytest_asyncio
from aiohttp import WSServerHandshakeError

from lerobot_webrtc.arms import AsyncArm, SimulatedArm
from lerobot_webrtc.models import arm_model
from lerobot_webrtc.peer import LABEL, Peer
from lerobot_webrtc.protocol import JOINTS, envelope
from lerobot_webrtc.runtime import run_control
from lerobot_webrtc.signaling import shutdown

from .conftest import TOKEN


class ControlledLeader(SimulatedArm):
    def __init__(self, model="so101"):
        super().__init__(model=model)
        values = (
            (9, -11, -13, 17, -19, 23, -25) if model == "b601-rs" else (9, -11, 13, -17, 19, 23)
        )
        self.action = dict(zip(self.position, values, strict=True))

    def get_action(self):
        return self.action.copy()


class RecordingFollower(SimulatedArm):
    def __init__(self, model="so101"):
        super().__init__(delta=3, model=model)
        self.positions = [self.position.copy()]
        self.targets = []

    def send_action(self, action):
        result = super().send_action(action)
        self.targets.append(action.copy())
        self.positions.append(result)
        return result


async def wait_for_control(predicate, tasks, timeout=5):
    async with asyncio.timeout(timeout):
        while not predicate():
            for task in tasks:
                if task.done():
                    task.result()
                    pytest.fail("Control endpoint exited before the expected motion")
            await asyncio.sleep(0.01)


@pytest_asyncio.fixture(params=["so100", "so101", "b601-rs"])
async def control_session(signaling_server, request):
    url, app = signaling_server
    leader = ControlledLeader(request.param)
    follower = RecordingFollower(request.param)
    leader_peer = Peer("leader", url, "acceptance", TOKEN)
    follower_peer = Peer("follower", url, "acceptance", TOKEN)
    tasks = [
        asyncio.create_task(
            run_control(AsyncArm(leader, "leader"), leader_peer, request.param, 30, 0.3, None)
        ),
        asyncio.create_task(
            run_control(AsyncArm(follower, "follower"), follower_peer, request.param, 30, 0.3, None)
        ),
    ]
    try:
        await wait_for_control(lambda: follower.applied >= 3, tasks)
        yield SimpleNamespace(
            leader=leader,
            follower=follower,
            leader_peer=leader_peer,
            follower_peer=follower_peer,
            tasks=tasks,
            app=app,
            model=request.param,
        )
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_every_joint_tracks_changing_leader_targets_with_relative_limits(control_session):
    session = control_session
    for peer in (session.leader_peer, session.follower_peer):
        assert peer.pc.connectionState == "connected"
        assert peer.pc.iceConnectionState == "completed"
        assert peer.pc.sctp.transport.state == "connected"
        assert peer.channel.label == LABEL
        assert peer.channel.readyState == "open"
        assert peer.channel.ordered is False
        assert peer.channel.maxRetransmits == 0
        assert peer.channel.maxPacketLifeTime is None

    values = (
        (-7, -5, -3, -11, 13, -3, -5) if session.model == "b601-rs" else (-7, 5, -3, 11, -13, 3)
    )
    for target in (
        session.leader.action.copy(),
        dict(zip(session.leader.action, values, strict=True)),
    ):
        session.leader.action = target
        await wait_for_control(
            lambda target=target: session.follower.position == target, session.tasks
        )
        assert target in session.follower.targets

    positions = session.follower.positions.copy()
    for previous, current in zip(positions[:-1], positions[1:], strict=True):
        for joint in arm_model(session.model).joints:
            assert abs(current[joint] - previous[joint]) <= session.follower.delta
    assert all(not task.done() for task in session.tasks)


async def assert_session_stopped(session, error_type, match=None):
    results = await asyncio.wait_for(
        asyncio.gather(*session.tasks, return_exceptions=True), timeout=3
    )
    assert isinstance(results[1], error_type), repr(results)
    if match is not None:
        assert match in str(results[1])
    assert all(isinstance(result, Exception) for result in results), repr(results)
    assert session.follower.held
    assert not session.follower.connected
    assert not session.leader.connected
    for peer in (session.leader_peer, session.follower_peer):
        assert peer.closed.is_set()
        assert peer.pc.connectionState == "closed"
        assert not peer.send("must not resume")
    writes = session.follower.applied
    await asyncio.sleep(0.1)
    assert session.follower.applied == writes


@pytest.mark.parametrize("failure", ["data-channel", "signaling"])
async def test_connection_loss_stops_both_endpoints_and_holds_follower(control_session, failure):
    session = control_session
    if failure == "data-channel":
        session.leader_peer.channel.close()
    else:
        await shutdown(session.app)
    await assert_session_stopped(session, ConnectionError)


async def test_silent_leader_trips_watchdog_and_cleans_up_full_session(
    control_session, monkeypatch
):
    session = control_session
    monkeypatch.setattr(session.leader_peer, "send", lambda message: False)
    await assert_session_stopped(session, TimeoutError, "watchdog")


@pytest.mark.parametrize("payload", ["invalid-json", "missing-joint", "out-of-range"])
async def test_invalid_command_over_webrtc_latches_shutdown(control_session, payload):
    session = control_session
    if payload == "invalid-json":
        message = "not JSON"
    else:
        action = session.leader.action.copy()
        if payload == "missing-joint":
            del action[JOINTS[0]]
        else:
            action[JOINTS[0]] = arm_model(session.model).bounds[0][2] + 1
        message = envelope(session.model, "action", permit="0" * 32, seq=100000, action=action)
    session.leader_peer.channel.send(message)
    await assert_session_stopped(session, ValueError)


async def test_wrong_token_never_connects_arm_or_applies_motion(signaling_server):
    url, _ = signaling_server
    device = RecordingFollower()
    peer = Peer("follower", url, "unauthorized", "wrong-token-at-least-16-characters")
    with pytest.raises(WSServerHandshakeError) as rejected:
        await asyncio.wait_for(
            run_control(AsyncArm(device, "follower"), peer, "so101", 30, 0.3, None), 3
        )
    assert rejected.value.status == 401
    assert device.applied == 0
    assert not device.connected
    assert peer.pc.connectionState == "closed"
