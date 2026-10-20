import asyncio
import time

import pytest

from lerobot_webrtc.arms import AsyncArm, SimulatedArm
from lerobot_webrtc.bench import run_bench
from lerobot_webrtc.models import arm_model
from lerobot_webrtc.peer import Peer
from lerobot_webrtc.protocol import FollowerInbox, LeaderInbox
from lerobot_webrtc.runtime import follower_loop, run_control

from .conftest import TOKEN


async def test_pending_command_is_consumed_before_blocking_telemetry():
    events = []
    position = dict.fromkeys(arm_model("b601-rs").joints, 0.0)
    inbox = FollowerInbox("b601-rs", 0.3)
    source = LeaderInbox("b601-rs")
    now = time.monotonic()
    source.receive(inbox.issue_permit(now, position))
    message = source.command(position)
    assert inbox.receive(message, now)
    command = inbox.pending
    inbox.pending = type(command)(command.action, now + 0.06)

    class PeerStub:
        closed = asyncio.Event()

        def send(self, message):
            events.append("permit")
            self.closed.set()

    class ArmStub:
        async def send(self, action, deadline, session_active):
            assert time.monotonic() < deadline
            events.append("motion")
            return True

        async def call(self, method):
            assert method == "get_observation"
            events.append("read")
            await asyncio.sleep(0.08)
            return position

    await follower_loop(ArmStub(), PeerStub(), inbox, 20)
    assert events == ["motion", "read", "permit"]


@pytest.mark.parametrize("initial_gripper", [0.0, 0.305421487])
async def test_b601_bench_observes_motion_and_return_over_real_webrtc(
    signaling_server, initial_gripper
):
    url, _ = signaling_server
    device = SimulatedArm(model="b601-rs")
    device.position["gripper.pos"] = initial_gripper
    follower_peer = Peer("follower", url, "b601-bench", TOKEN)
    leader_peer = Peer("leader", url, "b601-bench", TOKEN)
    follower = asyncio.create_task(
        run_control(AsyncArm(device, "follower"), follower_peer, "b601-rs", 30, 0.3, None)
    )
    try:
        result = await run_bench(leader_peer, 0.5, 3, simulated=True, require_return=True)
        assert result["simulated"] is True
        assert result["peak_directed_motion_deg"] >= 0.2
        assert result["returned_to_start_observed"]
        assert result["advancing_observations"] >= 10
        assert len(result["final"]) == 7
        assert result["final"]["wrist_yaw.pos"] == 0
        assert result["hold_targets"]["gripper.pos"] == 0
        assert result["final"]["gripper.pos"] == 0
        assert result["origin"]["gripper.pos"] == initial_gripper
        assert not result["physical_standstill_verified"]
        await asyncio.wait_for(asyncio.gather(follower, return_exceptions=True), 3)
    finally:
        follower.cancel()
        await asyncio.gather(follower, return_exceptions=True)
    assert device.held
    assert not device.connected


async def test_default_bench_moves_and_holds_without_requiring_home(signaling_server):
    url, _ = signaling_server
    device = SimulatedArm(model="b601-rs")
    follower_peer = Peer("follower", url, "b601-move-hold", TOKEN)
    leader_peer = Peer("leader", url, "b601-move-hold", TOKEN)
    follower = asyncio.create_task(
        run_control(AsyncArm(device, "follower"), follower_peer, "b601-rs", 30, 0.3, None)
    )
    try:
        result = await run_bench(leader_peer, 0.5, 3, simulated=True)
        assert result["return_required"] is False
        assert result["returned_to_start_observed"] is False
        assert result["final_offset_deg"] == pytest.approx(0.5)
        assert result["final_command"]["shoulder_pan.pos"] == pytest.approx(0.5)
        assert result["advancing_observations"] >= 10
    finally:
        follower.cancel()
        await asyncio.gather(follower, return_exceptions=True)
    assert device.held


async def test_commands_without_measured_motion_do_not_pass_b601_bench(signaling_server):
    class JammedSimulator(SimulatedArm):
        def send_action(self, action):
            self.applied += 1
            return self.position.copy()

    url, _ = signaling_server
    device = JammedSimulator(model="b601-rs")
    follower_peer = Peer("follower", url, "b601-jammed", TOKEN)
    leader_peer = Peer("leader", url, "b601-jammed", TOKEN)
    follower = asyncio.create_task(
        run_control(AsyncArm(device, "follower"), follower_peer, "b601-rs", 30, 0.3, None)
    )
    try:
        with pytest.raises(RuntimeError, match="encoder-observed"):
            await run_bench(leader_peer, 0.5, 3, simulated=True)
        assert device.applied > 10
    finally:
        follower.cancel()
        await asyncio.gather(follower, return_exceptions=True)


async def test_physical_bench_refuses_a_simulated_follower_without_commands(signaling_server):
    url, _ = signaling_server
    device = SimulatedArm(model="b601-rs")
    follower_peer = Peer("follower", url, "b601-identity", TOKEN)
    leader_peer = Peer("leader", url, "b601-identity", TOKEN)
    follower = asyncio.create_task(
        run_control(AsyncArm(device, "follower"), follower_peer, "b601-rs", 30, 0.3, None)
    )
    try:
        with pytest.raises(ValueError, match="no commands"):
            await run_bench(leader_peer, 0.5, 3, simulated=False)
        assert device.applied == 0
    finally:
        follower.cancel()
        await asyncio.gather(follower, return_exceptions=True)


async def test_wrong_model_is_refused_before_b601_device_connect(signaling_server):
    class DeviceThatMustNotConnect(SimulatedArm):
        def connect(self):
            pytest.fail("A mismatched source must be rejected before B601 hardware connect")

    url, _ = signaling_server
    device = DeviceThatMustNotConnect(model="b601-rs")
    follower_peer = Peer("follower", url, "b601-model", TOKEN)
    leader_peer = Peer("leader", url, "b601-model", TOKEN)
    tasks = [
        asyncio.create_task(
            run_control(AsyncArm(device, "follower"), follower_peer, "b601-rs", 30, 0.3, None)
        ),
        asyncio.create_task(
            run_control(AsyncArm(SimulatedArm(), "leader"), leader_peer, "so101", 30, 0.3, None)
        ),
    ]
    try:
        results = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 5)
        assert any(isinstance(result, ValueError) for result in results)
        assert device.applied == 0
        assert not device.connected
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
