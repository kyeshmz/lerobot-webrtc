"""Native host safety with memory drivers and local-only WebRTC, never hardware."""

import asyncio
import json
import threading
import time
from types import SimpleNamespace

import pytest

from lerobot_webrtc.arms import AsyncArm, SimulatedArm
from lerobot_webrtc.models import arm_model
from lerobot_webrtc.native_host import run_robot_host
from lerobot_webrtc.native_protocol import NATIVE_LABEL, native_message
from lerobot_webrtc.peer import Peer

from .conftest import TOKEN


class FakePeer:
    role = "follower"
    channel_label = NATIVE_LABEL
    video_source = None

    def __init__(self, receive=None):
        self.closed = asyncio.Event()
        self.error = None
        self.on_message = None
        self.receive = receive
        self.messages = []
        self.close_count = 0

    async def connect(self):
        assert callable(self.on_message)

    def send(self, message):
        if self.closed.is_set():
            return False
        data = json.loads(message)
        self.messages.append(data)
        if self.receive is not None:
            self.receive(data)
        elif data["type"] == "hello":
            self.reply(data, "ready", allow_physical=False)
        return True

    def reply(self, data, kind, **fields):
        self.on_message(native_message(data["model"], data["session"], kind, **fields))

    async def close(self):
        self.close_count += 1
        self.closed.set()


async def wait_for_message(peer, kind, *, after=0):
    async with asyncio.timeout(2):
        while True:
            matching = [data for data in peer.messages[after:] if data["type"] == kind]
            if matching:
                return matching[0]
            await asyncio.sleep(0.001)


def action(peer, state, seq=0, value=20):
    peer.reply(
        state,
        "action",
        seq=seq,
        permit=state["permit"],
        action=dict.fromkeys(arm_model(state["model"]).joints, value),
    )


async def stop(peer, task):
    state = next(data for data in peer.messages if data["type"] == "hello")
    peer.reply(state, "stop")
    await asyncio.wait_for(task, 2)


@pytest.mark.parametrize("model,value", [("so100", 20), ("so101", 20), ("b601-rs", 0)])
async def test_hello_measured_state_and_actual_clamped_result(model, value):
    class DistinctObservationArm(SimulatedArm):
        def get_observation(self):
            # Feedback deliberately differs from the driver's accepted target.
            observed = super().get_observation()
            observed["shoulder_pan.pos"] = -1
            return observed

    device = DistinctObservationArm(delta=2, model=model)
    peer = FakePeer()
    host = asyncio.create_task(
        run_robot_host(
            AsyncArm(device, "follower"),
            peer,
            model,
            100,
            0.3,
            None,
        )
    )
    try:
        state = await wait_for_message(peer, "state")
        hello = peer.messages[0]
        assert hello["type"] == "hello"
        assert hello["joints"] == list(arm_model(model).joints)
        assert hello["simulated"] is True
        assert hello["robot_id"] == "simulated"
        assert hello["timeout"] == 0.3
        assert hello["video"] is None
        assert len(hello["session"]) == 32
        assert state["position"]["shoulder_pan.pos"] == -1
        action(peer, state, value=value)
        result = await wait_for_message(peer, "result")
        assert result["seq"] == 0
        assert result["action"] == device.position
        assert result["action"] != state["position"]
        if value:
            assert result["action"] == dict.fromkeys(arm_model(model).joints, 2.0)
        assert device.applied == 1
        await stop(peer, host)
    finally:
        if not host.done():
            host.cancel()
        await asyncio.gather(host, return_exceptions=True)
    assert device.held and not device.connected
    assert peer.close_count == 1


async def test_hello_reports_video_metadata_without_reading_camera():
    peer = FakePeer()
    peer.video_source = SimpleNamespace(settings=SimpleNamespace(width=640, height=480, fps=15))
    host = asyncio.create_task(
        run_robot_host(
            AsyncArm(SimulatedArm(), "follower"),
            peer,
            "so101",
            100,
            0.3,
            0.03,
        )
    )
    await host
    assert peer.messages[0]["video"] == {"width": 640, "height": 480, "fps": 15}


@pytest.mark.parametrize("invalid", ["model", "units", "session", "permission", "type", "json"])
async def test_bad_handshake_never_connects_arm(invalid):
    class CountingArm(SimulatedArm):
        def connect(self):
            pytest.fail("Unverified network peer reached arm.connect")

    peer = FakePeer()

    def receive(hello):
        data = json.loads(native_message("so101", hello["session"], "ready", allow_physical=False))
        if invalid == "model":
            data["model"] = "so100"
        elif invalid == "units":
            data["units"] = "degrees"
        elif invalid == "session":
            data["session"] = "0" * 32
        elif invalid == "permission":
            data["allow_physical"] = 1
        elif invalid == "type":
            data["type"] = "state"
        peer.on_message("not json" if invalid == "json" else json.dumps(data))

    peer.receive = receive
    with pytest.raises(ValueError):
        await run_robot_host(AsyncArm(CountingArm(), "follower"), peer, "so101", 100, 0.3, None)
    assert peer.closed.is_set() and peer.close_count == 1


@pytest.mark.parametrize("allow", [False, True])
async def test_physical_permission_precedes_connection(allow):
    class PhysicalMemoryArm:
        model = "so101"

        def __init__(self):
            self.memory = SimulatedArm()
            self.connects = 0

        def connect(self):
            self.connects += 1
            self.memory.connect()

        def get_observation(self):
            return self.memory.get_observation()

        def hold(self):
            self.memory.hold()

        def disconnect(self):
            self.memory.disconnect()

    device = PhysicalMemoryArm()
    peer = FakePeer()
    peer.receive = lambda data: (
        peer.reply(data, "ready", allow_physical=allow) if data["type"] == "hello" else None
    )
    if allow:
        await run_robot_host(
            AsyncArm(device, "follower"),
            peer,
            "so101",
            100,
            0.3,
            0.03,
            robot_id="saved-calibration",
        )
    else:
        with pytest.raises(ValueError, match="allow_physical"):
            await run_robot_host(AsyncArm(device, "follower"), peer, "so101", 100, 0.3, None)
    assert device.connects == int(allow)
    assert peer.messages[0]["simulated"] is False


async def test_ready_duplicates_during_connect_do_not_grant_motion():
    class SlowConnect(SimulatedArm):
        def connect(self):
            started.set()
            release.wait()
            super().connect()

    started, release = threading.Event(), threading.Event()
    device = SlowConnect()
    peer = FakePeer()
    host = asyncio.create_task(
        run_robot_host(
            AsyncArm(device, "follower"),
            peer,
            "so101",
            100,
            0.3,
            None,
        )
    )
    try:
        assert await asyncio.to_thread(started.wait, 1)
        hello = peer.messages[0]
        for _ in range(3):
            peer.reply(hello, "ready", allow_physical=False)
        assert not peer.closed.is_set()
        assert not any(data["type"] == "state" for data in peer.messages)
        release.set()
        await wait_for_message(peer, "state")
        await stop(peer, host)
    finally:
        release.set()
        if not host.done():
            host.cancel()
        await asyncio.gather(host, return_exceptions=True)
    assert device.applied == 0


async def test_startup_watchdog_is_not_renewed_by_telemetry_or_ready():
    peer = FakePeer()

    def receive(data):
        if data["type"] in {"hello", "state"}:
            peer.reply(data, "ready", allow_physical=False)

    peer.receive = receive
    with pytest.raises(TimeoutError, match="watchdog"):
        await run_robot_host(
            AsyncArm(SimulatedArm(), "follower"),
            peer,
            "so101",
            200,
            0.3,
            None,
            startup_timeout=0.06,
        )
    assert len([data for data in peer.messages if data["type"] == "state"]) > 3


async def test_handshake_deadline_never_connects_arm():
    class CountingArm(SimulatedArm):
        def connect(self):
            pytest.fail("Unverified peer reached arm.connect")

    peer = FakePeer(receive=lambda data: None)
    with pytest.raises(TimeoutError):
        await run_robot_host(
            AsyncArm(CountingArm(), "follower"), peer, "so101", 200, 0.3, None, startup_timeout=0.03
        )
    assert peer.close_count == 1
    assert all(data["type"] == "hello" for data in peer.messages)


async def test_standalone_channel_is_rejected_before_arm_connection():
    class CountingArm(SimulatedArm):
        def connect(self):
            pytest.fail("Legacy channel reached arm.connect")

    peer = FakePeer()
    peer.channel_label = "lerobot-actions-v1"
    with pytest.raises(ValueError, match="native data channel"):
        await run_robot_host(AsyncArm(CountingArm(), "follower"), peer, "so101", 100, 0.3, None)
    assert peer.messages == []


async def test_fresh_permit_packet_cannot_revive_expired_active_watchdog():
    device = SimulatedArm()
    peer = FakePeer()
    host = asyncio.create_task(
        run_robot_host(
            AsyncArm(device, "follower"),
            peer,
            "so101",
            200,
            0.1,
            None,
        )
    )
    try:
        action(peer, await wait_for_message(peer, "state"))
        await wait_for_message(peer, "result")
        await asyncio.sleep(0.06)
        latest = next(data for data in reversed(peer.messages) if data["type"] == "state")
        # Deliver the packet before the host loop can run its next timer check.
        # Its newer telemetry permit is still fresh, but motion has gone idle.
        time.sleep(0.05)
        action(peer, latest, seq=1)
        assert peer.closed.is_set()
        with pytest.raises(TimeoutError, match="watchdog"):
            await asyncio.wait_for(host, 1)
        assert device.applied == 1
    finally:
        if not host.done():
            host.cancel()
        await asyncio.gather(host, return_exceptions=True)


async def test_slow_feedback_checks_watchdog_before_issuing_new_state():
    class SlowFeedback(SimulatedArm):
        def get_observation(self):
            if self.applied:
                time.sleep(0.06)
            return super().get_observation()

    device = SlowFeedback()
    peer = FakePeer()
    host = asyncio.create_task(
        run_robot_host(
            AsyncArm(device, "follower"),
            peer,
            "so101",
            200,
            0.04,
            None,
        )
    )
    state = await wait_for_message(peer, "state")
    action(peer, state)
    with pytest.raises(TimeoutError, match="watchdog"):
        await asyncio.wait_for(host, 1)
    assert device.applied == 1
    receipt_index = next(i for i, data in enumerate(peer.messages) if data["type"] == "result")
    assert not any(data["type"] == "state" for data in peer.messages[receipt_index:])


async def test_stop_during_connect_joins_and_holds_without_issuing_state():
    class ConnectingArm(SimulatedArm):
        def connect(self):
            started.set()
            release.wait()
            super().connect()

    started, release = threading.Event(), threading.Event()
    device = ConnectingArm()
    peer = FakePeer()
    host = asyncio.create_task(
        run_robot_host(
            AsyncArm(device, "follower"),
            peer,
            "so101",
            200,
            0.3,
            None,
        )
    )
    try:
        assert await asyncio.to_thread(started.wait, 1)
        peer.reply(peer.messages[0], "stop")
        release.set()
        await asyncio.wait_for(host, 1)
        assert device.held and not device.connected
        assert not any(data["type"] == "state" for data in peer.messages)
    finally:
        release.set()
        if not host.done():
            host.cancel()
        await asyncio.gather(host, return_exceptions=True)


async def test_cached_receipt_recovers_loss_without_reapplication_or_watchdog_renewal():
    device = SimulatedArm()
    peer = FakePeer()
    host = asyncio.create_task(
        run_robot_host(
            AsyncArm(device, "follower"),
            peer,
            "so101",
            200,
            0.06,
            None,
        )
    )
    state = await wait_for_message(peer, "state")
    action(peer, state)
    with pytest.raises(TimeoutError, match="watchdog"):
        await asyncio.wait_for(host, 1)
    results = [data for data in peer.messages if data["type"] == "result"]
    assert len(results) >= 3
    assert all(data == results[0] for data in results)
    assert device.applied == 1
    assert device.held and not device.connected


@pytest.mark.parametrize("invalid", ["expired", "unknown", "out_of_order", "malformed"])
async def test_invalid_action_terminally_stops_without_resume(invalid):
    device = SimulatedArm()
    peer = FakePeer()
    host = asyncio.create_task(
        run_robot_host(
            AsyncArm(device, "follower"),
            peer,
            "so101",
            200,
            0.05,
            None,
        )
    )
    state = await wait_for_message(peer, "state")
    if invalid == "expired":
        await asyncio.sleep(0.07)
    elif invalid == "out_of_order":
        action(peer, state)
        await wait_for_message(peer, "result")
    if invalid == "unknown":
        state = {**state, "permit": "0" * 32}
    if invalid == "malformed":
        peer.reply(state, "action", seq=0, permit=state["permit"], action={})
    else:
        action(peer, state)
    with pytest.raises((TimeoutError, ValueError)):
        await asyncio.wait_for(host, 1)
    assert peer.closed.is_set()
    assert device.applied == int(invalid == "out_of_order")
    action(peer, state, seq=2)
    await asyncio.sleep(0.01)
    assert device.applied == int(invalid == "out_of_order")


async def test_overlapping_action_revokes_inflight_write_and_never_acks_it():
    class BlockingArm(SimulatedArm):
        def send_action(self, action):
            started.set()
            release.wait()
            return super().send_action(action)

    started, release = threading.Event(), threading.Event()
    device = BlockingArm()
    peer = FakePeer()
    host = asyncio.create_task(
        run_robot_host(
            AsyncArm(device, "follower"),
            peer,
            "so101",
            200,
            0.3,
            None,
        )
    )
    try:
        state = await wait_for_message(peer, "state")
        action(peer, state)
        assert await asyncio.to_thread(started.wait, 1)
        action(peer, state, seq=1)
        assert peer.closed.is_set()
        release.set()
        with pytest.raises(ValueError, match="overlapped"):
            await asyncio.wait_for(host, 1)
        assert not any(data["type"] == "result" for data in peer.messages)
        assert device.applied == 1
    finally:
        release.set()
        if not host.done():
            host.cancel()
        await asyncio.gather(host, return_exceptions=True)


async def test_queued_action_expires_before_worker_and_is_not_written():
    class BlockingArm(SimulatedArm):
        def block(self):
            started.set()
            release.wait()

    started, release = threading.Event(), threading.Event()
    device = BlockingArm()
    arm = AsyncArm(device, "follower")
    peer = FakePeer()
    blocker = None

    def receive(data):
        nonlocal blocker
        if data["type"] == "hello":
            peer.reply(data, "ready", allow_physical=False)
        elif data["type"] == "state" and blocker is None:
            # Occupy the same executor ahead of the next native send.
            blocker = asyncio.create_task(arm.call("block"))
            action(peer, data)

    peer.receive = receive
    host = asyncio.create_task(run_robot_host(arm, peer, "so101", 200, 0.04, None))
    try:
        assert await asyncio.to_thread(started.wait, 1)
        await asyncio.sleep(0.06)
        release.set()
        with pytest.raises(TimeoutError, match="deadline"):
            await asyncio.wait_for(host, 1)
        assert device.applied == 0
        assert not any(data["type"] == "result" for data in peer.messages)
    finally:
        release.set()
        if not host.done():
            host.cancel()
        await asyncio.gather(host, return_exceptions=True)
        if blocker is not None:
            await blocker


async def test_write_overrun_never_emits_applied_receipt():
    class SlowWrite(SimulatedArm):
        def send_action(self, action):
            time.sleep(0.06)
            return super().send_action(action)

    device = SlowWrite()
    peer = FakePeer()
    host = asyncio.create_task(
        run_robot_host(
            AsyncArm(device, "follower"),
            peer,
            "so101",
            200,
            0.04,
            None,
        )
    )
    action(peer, await wait_for_message(peer, "state"))
    with pytest.raises(TimeoutError, match="deadline"):
        await host
    assert device.applied == 1
    assert not any(data["type"] == "result" for data in peer.messages)
    assert device.held and not device.connected


async def test_none_driver_result_is_uncertain_and_terminal_not_an_echo():
    class NoResult(SimulatedArm):
        def send_action(self, action):
            super().send_action(action)

    peer = FakePeer()
    host = asyncio.create_task(
        run_robot_host(
            AsyncArm(NoResult(), "follower"),
            peer,
            "so101",
            200,
            0.3,
            None,
        )
    )
    action(peer, await wait_for_message(peer, "state"))
    with pytest.raises(ValueError, match="exactly"):
        await host
    assert not any(data["type"] == "result" for data in peer.messages)


async def test_stop_cancellation_joins_io_then_holds_before_media_cleanup_once():
    events = []

    class BlockingArm(SimulatedArm):
        def send_action(self, action):
            started.set()
            release.wait()
            events.append("write-done")
            return super().send_action(action)

        def hold(self):
            assert peer.closed.is_set()
            events.append("hold")
            super().hold()

        def disconnect(self):
            events.append("disconnect")
            super().disconnect()

    class MediaPeer(FakePeer):
        async def close(self):
            events.append("media-close")
            closing.set()
            await release_close.wait()
            await super().close()

    started, release = threading.Event(), threading.Event()
    closing, release_close = asyncio.Event(), asyncio.Event()
    device = BlockingArm()
    arm = AsyncArm(device, "follower")
    peer = MediaPeer()
    host = asyncio.create_task(run_robot_host(arm, peer, "so101", 200, 0.3, None))
    try:
        state = await wait_for_message(peer, "state")
        action(peer, state)
        assert await asyncio.to_thread(started.wait, 1)
        peer.reply(state, "stop")
        host.cancel()
        await asyncio.sleep(0)
        host.cancel()
        await asyncio.sleep(0.01)
        assert peer.closed.is_set() and not host.done() and events == []
        release.set()
        await asyncio.wait_for(closing.wait(), 1)
        assert events == ["write-done", "hold", "disconnect", "media-close"]
        host.cancel()
        await asyncio.sleep(0)
        assert not host.done()
        release_close.set()
        with pytest.raises(asyncio.CancelledError):
            await host
        await arm.close()
        assert peer.close_count == 1
        assert not any(data["type"] == "result" for data in peer.messages)
    finally:
        release.set()
        release_close.set()
        if not host.done():
            host.cancel()
        await asyncio.gather(host, return_exceptions=True)


async def test_real_local_native_channel(signaling_server):
    url, _ = signaling_server
    leader = Peer("leader", url, "native-host", TOKEN, channel_label=NATIVE_LABEL)
    follower = Peer("follower", url, "native-host", TOKEN, channel_label=NATIVE_LABEL)
    device = SimulatedArm(delta=2)
    result = asyncio.Future()
    submitted = False

    def receive(message):
        nonlocal submitted
        data = json.loads(message)
        if data["type"] == "hello":
            leader.send(native_message("so101", data["session"], "ready", allow_physical=False))
        elif data["type"] == "state" and not submitted:
            submitted = leader.send(
                native_message(
                    "so101",
                    data["session"],
                    "action",
                    seq=0,
                    permit=data["permit"],
                    action=dict.fromkeys(device.position, 20),
                )
            )
        elif data["type"] == "result" and not result.done():
            result.set_result(data)

    leader.on_message = receive
    host = asyncio.create_task(
        run_robot_host(
            AsyncArm(device, "follower"),
            follower,
            "so101",
            30,
            0.3,
            None,
        )
    )
    try:
        await leader.connect(10)
        receipt = await asyncio.wait_for(result, 3)
        assert receipt["action"] == dict.fromkeys(device.position, 2.0)
        assert follower.channel.label == NATIVE_LABEL
        assert follower.channel.ordered is False
        assert follower.channel.maxRetransmits == 0
        assert device.applied == 1
        leader.send(native_message("so101", receipt["session"], "stop"))
        await asyncio.wait_for(host, 2)
    finally:
        if not host.done():
            host.cancel()
        await asyncio.gather(host, return_exceptions=True)
        await leader.close()
    assert device.held and not device.connected
