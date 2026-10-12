import asyncio
import time

import pytest

from lerobot_webrtc.arms import AsyncArm, SimulatedArm
from lerobot_webrtc.peer import Peer
from lerobot_webrtc.protocol import FollowerInbox, LeaderInbox
from lerobot_webrtc.runtime import follower_loop, leader_loop, run_control

from .conftest import TOKEN


async def test_real_webrtc_controls_simulated_arm(signaling_server):
    url, _ = signaling_server
    leader_peer = Peer("leader", url, "integration", TOKEN)
    follower_peer = Peer("follower", url, "integration", TOKEN)
    leader = AsyncArm(SimulatedArm(), "leader")
    device = SimulatedArm()
    follower = AsyncArm(device, "follower")
    tasks = [
        asyncio.create_task(run_control(leader, leader_peer, "so101", 30, 0.3, None)),
        asyncio.create_task(run_control(follower, follower_peer, "so101", 30, 0.3, None)),
    ]
    try:
        async with asyncio.timeout(10):
            while device.applied < 8:
                for task in tasks:
                    if task.done():
                        task.result()
                await asyncio.sleep(0.01)
        assert device.position["gripper.pos"] == 25
        assert follower_peer.channel.ordered is False
        assert follower_peer.channel.maxRetransmits == 0
        assert leader_peer.pc.connectionState == "connected"
        assert follower_peer.pc.connectionState == "connected"
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    assert not device.connected
    assert device.held


async def test_watchdog_latches_after_leader_stops(signaling_server):
    url, _ = signaling_server
    leader_peer = Peer("leader", url, "watchdog", TOKEN)
    follower_peer = Peer("follower", url, "watchdog", TOKEN)
    leader = AsyncArm(SimulatedArm(), "leader")
    device = SimulatedArm()
    follower = AsyncArm(device, "follower")
    leader_inbox = LeaderInbox("so101")
    follower_inbox = FollowerInbox("so101", 0.2)
    leader_peer.on_message = leader_inbox.receive
    follower_peer.on_message = lambda message: follower_inbox.receive(message, time.monotonic())
    tasks = []
    try:
        await asyncio.gather(leader_peer.connect(10), follower_peer.connect(10))
        await asyncio.gather(leader.call("connect"), follower.call("connect"))
        leader_task = asyncio.create_task(leader_loop(leader, leader_peer, leader_inbox, 30))
        follower_task = asyncio.create_task(
            follower_loop(follower, follower_peer, follower_inbox, 30)
        )
        tasks = [leader_task, follower_task]
        async with asyncio.timeout(5):
            while device.applied < 3:
                await asyncio.sleep(0.01)
        leader_task.cancel()
        await asyncio.gather(leader_task, return_exceptions=True)
        with pytest.raises(TimeoutError, match="watchdog"):
            await asyncio.wait_for(follower_task, timeout=2)
        writes = device.applied
        await asyncio.sleep(0.1)
        assert device.applied == writes
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.gather(leader_peer.close(), follower_peer.close())
        await asyncio.gather(leader.close(), follower.close())


async def test_expired_command_is_not_written_by_worker():
    device = SimulatedArm()
    arm = AsyncArm(device, "follower")
    try:
        await arm.call("connect")
        assert not await arm.send(device.position, time.monotonic() - 1)
        assert device.applied == 0
    finally:
        await arm.close()


async def test_slow_successful_send_overrun_latches_watchdog():
    from types import SimpleNamespace

    class PeerStub:
        def __init__(self):
            self.closed = asyncio.Event()

        def send(self, message):
            pass

    class InboxStub:
        timeout = 1

        def __init__(self):
            self.command = SimpleNamespace(action={}, deadline=time.monotonic() + 0.03)

        def issue_permit(self, now):
            return "permit"

        def take(self, now):
            command, self.command = self.command, None
            return command

    class SlowSendArm:
        async def send(self, action, deadline, session_active):
            await asyncio.sleep(0.05)
            return True

    peer = PeerStub()
    with pytest.raises(TimeoutError, match="overran"):
        await follower_loop(SlowSendArm(), peer, InboxStub(), 100, startup_timeout=1)
    assert peer.closed.is_set()


async def test_queued_worker_admission_uses_watchdog_deadline():
    import threading
    from types import SimpleNamespace

    class BlockingArm(SimulatedArm):
        def send_action(self, action):
            result = super().send_action(action)
            if self.applied == 1:
                self.first_write.set()
            return result

        def block(self):
            self.worker_started.set()
            self.worker_release.wait()

    class PeerStub:
        def __init__(self):
            self.closed = asyncio.Event()

        def send(self, message):
            pass

    class InboxStub:
        timeout = 0.15

        def __init__(self, action):
            self.commands = [
                SimpleNamespace(action=action, deadline=time.monotonic() + 1),
                SimpleNamespace(action=action, deadline=time.monotonic() + 1),
            ]

        def issue_permit(self, now):
            return "permit"

        def take(self, now):
            return self.commands.pop(0) if self.commands else None

    device = BlockingArm()
    device.first_write = threading.Event()
    device.worker_started = threading.Event()
    device.worker_release = threading.Event()
    arm = AsyncArm(device, "follower")
    peer = PeerStub()
    loop = asyncio.create_task(
        follower_loop(arm, peer, InboxStub(device.position.copy()), 100, startup_timeout=1)
    )
    try:
        assert await asyncio.to_thread(device.first_write.wait, 1)
        blocker = asyncio.create_task(arm.call("block"))
        assert await asyncio.to_thread(device.worker_started.wait, 1)
        await asyncio.sleep(0.17)
        device.worker_release.set()
        await blocker
        with pytest.raises(TimeoutError, match="watchdog"):
            await asyncio.wait_for(loop, 1)
        assert device.applied == 1
        assert peer.closed.is_set()
    finally:
        device.worker_release.set()
        if not loop.done():
            loop.cancel()
        await asyncio.gather(loop, return_exceptions=True)
        await arm.close()


async def test_closed_session_rejects_write_waiting_for_worker():
    class BlockingArm(SimulatedArm):
        def block(self):
            self.started.set()
            self.release.wait()

    device = BlockingArm()
    import threading

    device.started = threading.Event()
    device.release = threading.Event()
    arm = AsyncArm(device, "follower")
    active = True
    try:
        blocker = asyncio.create_task(arm.call("block"))
        assert await asyncio.to_thread(device.started.wait, 1)
        send = asyncio.create_task(
            arm.send(device.position, time.monotonic() + 5, session_active=lambda: active)
        )
        await asyncio.sleep(0)
        active = False
        device.release.set()
        await blocker
        assert not await send
        assert device.applied == 0
    finally:
        device.release.set()
        await arm.close()


async def test_queued_write_rechecks_deadline_at_worker_boundary():
    class BlockingArm(SimulatedArm):
        def block(self):
            self.started.set()
            self.release.wait()

    import threading

    device = BlockingArm()
    device.started = threading.Event()
    device.release = threading.Event()
    arm = AsyncArm(device, "follower")
    try:
        blocker = asyncio.create_task(arm.call("block"))
        assert await asyncio.to_thread(device.started.wait, 1)
        send = asyncio.create_task(arm.send(device.position, time.monotonic() + 0.03))
        await asyncio.sleep(0.05)
        device.release.set()
        await blocker
        assert not await send
        assert device.applied == 0
    finally:
        device.release.set()
        await arm.close()


async def test_cancelled_queued_write_is_revoked():
    import threading

    class BlockingArm(SimulatedArm):
        def block(self):
            self.started.set()
            self.release.wait()

    device = BlockingArm()
    device.started, device.release = threading.Event(), threading.Event()
    arm = AsyncArm(device, "follower")
    blocker = asyncio.create_task(arm.call("block"))
    assert await asyncio.to_thread(device.started.wait, 1)
    send = asyncio.create_task(arm.send(device.position, time.monotonic() + 5))
    await asyncio.sleep(0.01)
    send.cancel()
    await asyncio.sleep(0.01)
    device.release.set()
    await blocker
    with pytest.raises(asyncio.CancelledError):
        await send
    assert device.applied == 0
    await arm.close()


async def test_cancellation_waits_for_serial_operation():
    class SlowArm(SimulatedArm):
        def get_action(self):
            time.sleep(0.08)
            self.read_finished = True
            return super().get_action()

    device = SlowArm()
    device.read_finished = False
    arm = AsyncArm(device, "leader")
    try:
        read = asyncio.create_task(arm.call("get_action"))
        await asyncio.sleep(0.01)
        read.cancel()
        with pytest.raises(asyncio.CancelledError):
            await read
        assert device.read_finished
    finally:
        await arm.close()


async def test_repeated_cancellation_still_joins_executor_call():
    import threading

    class BlockingArm(SimulatedArm):
        def get_action(self):
            self.started.set()
            self.release.wait()
            self.finished.set()
            return super().get_action()

    device = BlockingArm()
    device.started, device.release, device.finished = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )
    arm = AsyncArm(device, "leader")
    task = asyncio.create_task(arm.call("get_action"))
    assert await asyncio.to_thread(device.started.wait, 1)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    device.release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    assert device.finished.is_set()
    await arm.close()


async def test_model_mismatch_ends_session_without_motion(signaling_server):
    url, _ = signaling_server
    device = SimulatedArm()
    leader_peer = Peer("leader", url, "mismatch", TOKEN)
    follower_peer = Peer("follower", url, "mismatch", TOKEN)
    tasks = [
        asyncio.create_task(
            run_control(AsyncArm(SimulatedArm(), "leader"), leader_peer, "so100", 30, 0.3, None)
        ),
        asyncio.create_task(
            run_control(AsyncArm(device, "follower"), follower_peer, "so101", 30, 0.3, None)
        ),
    ]
    try:
        results = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 10)
        assert any(isinstance(result, ValueError) for result in results)
        assert device.applied == 0
        assert device.held
        assert not device.connected
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_partial_setup_failure_closes_arm_before_peer():
    class SetupArm(SimulatedArm):
        def connect(self):
            self.connected = True
            raise RuntimeError("setup failed")

        def hold(self):
            self.held = True
            raise RuntimeError("hold failed")

    class StubPeer:
        role = "leader"

        def __init__(self):
            self.closed = asyncio.Event()
            self.order = []

        async def connect(self):
            self.order.append("connect")

        async def close(self):
            self.order.append("peer-close")

    peer = StubPeer()
    device = SetupArm()
    arm = AsyncArm(device, "follower")
    with pytest.raises(BaseExceptionGroup, match="Session failed during shutdown") as caught:
        await run_control(arm, peer, "so101", 30, 0.3, None)
    assert "setup failed" in repr(caught.value)
    assert "hold failed" in repr(caught.value)
    assert peer.order == ["connect", "peer-close"]
    assert device.held
    assert not device.connected


async def test_duration_stop_latches_before_cleanup():
    import threading

    events = []

    class StubPeer:
        role = "leader"

        def __init__(self):
            self.closed = asyncio.Event()
            self.error = None
            self.order = []

        async def connect(self):
            events.append("connect")

        async def close(self):
            events.append("network-close")

        def send(self, message):
            pass

    class BlockingHoldArm(SimulatedArm):
        def hold(self):
            events.append("hold-start")
            self.hold_started.set()
            self.release_hold.wait()
            events.append("hold-done")

        def disconnect(self):
            events.append("disconnect")
            super().disconnect()

    device = BlockingHoldArm()
    device.hold_started, device.release_hold = threading.Event(), threading.Event()
    peer = StubPeer()
    arm = AsyncArm(device, "follower")
    task = asyncio.create_task(run_control(arm, peer, "so101", 30, 0.3, 0.01))
    assert await asyncio.to_thread(device.hold_started.wait, 1)
    assert peer.closed.is_set()
    task.cancel()
    task.cancel()
    await asyncio.sleep(0.02)
    assert events == ["connect", "hold-start"]
    device.release_hold.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    assert events == ["connect", "hold-start", "hold-done", "disconnect", "network-close"]


async def test_owner_joins_actual_peer_teardown_across_repeated_cancellation():
    peer = Peer("leader", "ws://unused", "cancel-cleanup", TOKEN)
    entered_close = asyncio.Event()
    release_close = asyncio.Event()
    original_close = peer.pc.close

    async def delayed_close():
        entered_close.set()
        await release_close.wait()
        await original_close()

    peer.pc.close = delayed_close

    async def connected():
        return None

    peer.connect = connected
    task = asyncio.create_task(
        run_control(AsyncArm(SimulatedArm(), "leader"), peer, "so101", 30, 0.3, 0.01)
    )
    try:
        await asyncio.wait_for(entered_close.wait(), 1)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0.02)
        assert not task.done()
        release_close.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        assert peer.pc.connectionState == "closed"
        assert not [
            pending
            for pending in asyncio.all_tasks()
            if pending.get_name() == "peer-close" and not pending.done()
        ]
    finally:
        release_close.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_duration_stop_revokes_follower_write_queued_behind_worker():
    import threading

    events = []

    class QueuedArm(SimulatedArm):
        def block(self):
            self.started.set()
            self.release.wait()

        def send_action(self, action):
            events.append("motion")
            return super().send_action(action)

        def hold(self):
            events.append("hold")
            super().hold()

        def disconnect(self):
            events.append("disconnect")
            super().disconnect()

    device = QueuedArm()
    device.started, device.release = threading.Event(), threading.Event()
    arm = AsyncArm(device, "follower")

    class StubPeer:
        role = "follower"

        def __init__(self):
            self.closed = asyncio.Event()
            self.error = None
            self.on_message = None

        async def connect(self):
            self.blocker = asyncio.create_task(arm.call("block"))

        def send(self, permit_message):
            leader = LeaderInbox("so101")
            leader.receive(permit_message)
            action = leader.command(dict.fromkeys(device.position, 1.0))
            self.on_message(action)

        async def close(self):
            events.append("network-close")

    peer = StubPeer()
    task = asyncio.create_task(run_control(arm, peer, "so101", 30, 0.3, 0.03))
    assert await asyncio.to_thread(device.started.wait, 1)
    await asyncio.sleep(0.06)
    assert peer.closed.is_set()
    assert events == []
    device.release.set()
    await asyncio.wait_for(task, 1)
    await peer.blocker
    assert events == ["hold", "disconnect", "network-close"]


@pytest.mark.parametrize("failure", ["hold", "disconnect"])
async def test_close_reports_cleanup_failures_and_attempts_both(failure):
    events = []

    class FailingArm(SimulatedArm):
        def hold(self):
            events.append("hold")
            if failure == "hold":
                raise RuntimeError("hold failed")

        def disconnect(self):
            events.append("disconnect")
            if failure == "disconnect":
                raise RuntimeError("disconnect failed")

    arm = AsyncArm(FailingArm(), "follower")
    with pytest.raises(ExceptionGroup, match="cleanup"):
        await arm.close()
    assert events == ["hold", "disconnect"]


async def test_concurrent_close_runs_actuator_cleanup_once():
    events = []

    class CountingArm(SimulatedArm):
        def hold(self):
            events.append("hold")

        def disconnect(self):
            events.append("disconnect")

    arm = AsyncArm(CountingArm(), "follower")
    await asyncio.gather(arm.close(), arm.close(), arm.close())
    await arm.close()
    assert events == ["hold", "disconnect"]


async def test_send_after_close_started_during_blocked_hold_is_rejected():
    import threading

    events = []

    class BlockingHoldArm(SimulatedArm):
        def hold(self):
            events.append("hold-start")
            self.started.set()
            self.release.wait()

        def send_action(self, action):
            events.append("motion")
            return super().send_action(action)

        def disconnect(self):
            events.append("disconnect")
            super().disconnect()

    device = BlockingHoldArm()
    device.started, device.release = threading.Event(), threading.Event()
    arm = AsyncArm(device, "follower")
    close_task = asyncio.create_task(arm.close())
    assert await asyncio.to_thread(device.started.wait, 1)
    assert await arm.send(device.position, time.monotonic() + 2) is False
    assert events == ["hold-start"]
    device.release.set()
    await asyncio.wait_for(close_task, 1)
    assert events == ["hold-start", "disconnect"]
    assert await arm.send(device.position, time.monotonic() + 2) is False
    assert events == ["hold-start", "disconnect"]


async def test_send_queued_before_close_is_rejected_at_worker_boundary():
    import threading

    events = []

    class BlockingWorkerArm(SimulatedArm):
        def block(self):
            self.started.set()
            self.release.wait()

        def send_action(self, action):
            events.append("motion")
            return super().send_action(action)

        def hold(self):
            events.append("hold")

        def disconnect(self):
            events.append("disconnect")

    device = BlockingWorkerArm()
    device.started, device.release = threading.Event(), threading.Event()
    arm = AsyncArm(device, "follower")
    blocker = asyncio.create_task(arm.call("block"))
    assert await asyncio.to_thread(device.started.wait, 1)
    send = asyncio.create_task(arm.send(device.position, time.monotonic() + 2))
    await asyncio.sleep(0)
    close_task = asyncio.create_task(arm.close())
    # Wait for close admission to latch before releasing the executor worker.
    async with asyncio.timeout(1):
        while not arm._closing:
            await asyncio.sleep(0)
    device.release.set()
    await blocker
    assert await send is False
    await asyncio.wait_for(close_task, 1)
    assert events == ["hold", "disconnect"]
