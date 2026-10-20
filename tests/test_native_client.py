"""Native client lifecycle, pinning and fail-closed RPC without SDK/hardware I/O."""

import asyncio
import json
import logging
import threading
import time
from types import SimpleNamespace

import av
import pytest

from lerobot_webrtc import native_client
from lerobot_webrtc.models import arm_model
from lerobot_webrtc.native_client import RemoteRobotClient
from lerobot_webrtc.native_options import RemoteRobotOptions
from lerobot_webrtc.native_protocol import NATIVE_LABEL, native_message

from .conftest import TOKEN

SESSION = "a" * 32
PERMIT = "b" * 32


def positions(model="so101"):
    return {joint: (low + high) / 2 for joint, low, high in arm_model(model).bounds}


class FakeReceiver:
    def __init__(self, options, scenario):
        self.settings = options.video_settings()
        self.scenario = scenario
        self.ready = asyncio.Event()
        self.signaling_ready = asyncio.Event()
        self.frame = None
        self.received_at = 0
        self.frames_received = 0
        self.closed = False
        self.on_error = None

    def receive_frame(self):
        self.frame = av.VideoFrame(self.settings.width, self.settings.height, "yuv420p")
        self.received_at = time.monotonic()
        self.frames_received += 1
        self.ready.set()

    async def start(self):
        self.signaling_ready.set()
        delay = getattr(self.scenario, "frame_delay", 0)
        if delay:
            asyncio.get_running_loop().call_later(delay, self.receive_frame)
        elif not getattr(self.scenario, "no_frame", False):
            self.receive_frame()

    async def close(self):
        self.closed = True


class FakePeer:
    def __init__(self, scenario, role, signaling, room, token, ice_servers, **kwargs):
        self.scenario = scenario
        self.role = role
        self.token = token
        self.ice_servers = ice_servers
        self.channel_label = kwargs["channel_label"]
        self.receiver = kwargs["video_receiver"]
        self.closed = asyncio.Event()
        self.opened = asyncio.Event()
        self.error = None
        self.on_message = None
        self.sent = []
        self.actions = []
        self.ready_count = 0
        self.state_sent = False
        self.close_started = threading.Event()
        self.close_finished = threading.Event()
        self.network_thread = threading.current_thread()
        scenario.peer = self

    def emit(self, kind, **fields):
        self.on_message(native_message(self.scenario.model, SESSION, kind, **fields))

    async def connect(self, timeout):
        if self.receiver is not None:
            await self.receiver.start()
        if getattr(self.scenario, "partial_error", False):
            raise RuntimeError(f"Do not expose credentials: {self.token}")
        self.opened.set()
        hello = {
            "joints": list(arm_model(self.scenario.model).joints),
            "simulated": True,
            "robot_id": "simulated",
            "timeout": 0.4,
            "video": None
            if self.receiver is None
            else {key: getattr(self.receiver.settings, key) for key in ("width", "height", "fps")},
        }
        hello.update(getattr(self.scenario, "hello", {}))
        data = json.loads(native_message(self.scenario.model, SESSION, "hello", **hello))
        data.update(getattr(self.scenario, "envelope", {}))
        self.on_message(json.dumps(data))
        if self.receiver is not None:
            await self.receiver.ready.wait()

    def send(self, message):
        assert threading.current_thread() is self.network_thread
        data = json.loads(message)
        self.sent.append(data)
        if data["type"] == "ready":
            self.ready_count += 1
            if self.ready_count <= getattr(self.scenario, "ready_drops", 0):
                return False
            if not self.state_sent and not getattr(self.scenario, "no_state", False):
                self.state_sent = True
                self.emit("state", seq=0, position=positions(self.scenario.model), permit=PERMIT)
        elif data["type"] == "action":
            self.actions.append(data)
            if getattr(self.scenario, "congested", False):
                return False
            on_action = getattr(self.scenario, "on_action", None)
            if on_action is not None:
                on_action(self, data)
            elif not getattr(self.scenario, "no_result", False):
                actual = dict(data["action"])
                actual["shoulder_pan.pos"] -= 1
                self.emit("result", seq=data["seq"], action=actual)
        return True

    async def close(self):
        self.close_started.set()
        self.closed.set()
        gate = getattr(self.scenario, "close_gate", None)
        while gate is not None and not gate.is_set():
            await asyncio.sleep(0.002)
        if self.receiver is not None:
            await self.receiver.close()
        self.close_finished.set()


@pytest.fixture
def make_client(monkeypatch):
    monkeypatch.setenv("LEROBOT_WEBRTC_TOKEN", TOKEN)
    clients = []

    def make(*, connect=True, behavior=None, **options):
        scenario = SimpleNamespace(model=options.get("model", "so101"), **(behavior or {}))
        monkeypatch.setattr(
            native_client,
            "Peer",
            lambda *args, **kwargs: FakePeer(
                scenario,
                *args,
                **kwargs,
            ),
        )
        monkeypatch.setattr(
            native_client,
            "_make_video_receiver",
            lambda opts: FakeReceiver(
                opts,
                scenario,
            ),
        )
        settings = {
            "connect_timeout": 0.5,
            "request_timeout": 0.1,
            "observation_timeout": 0.2,
            **options,
        }
        client = RemoteRobotClient(RemoteRobotOptions(**settings))
        clients.append(client)
        if connect:
            client.connect()
        return client, scenario

    yield make
    for client in clients:
        client.disconnect()


def on_loop(client, callback):
    """Execute on the real network loop; never fake monotonic time/deadlines."""
    completed = threading.Event()
    errors = []

    def run():
        try:
            callback()
        except BaseException as exc:
            errors.append(exc)
        finally:
            completed.set()

    client._loop.call_soon_threadsafe(run)
    assert completed.wait(2)
    if errors:
        raise errors[0]


def assert_closed(client, peer):
    client.disconnect()
    assert not client.is_connected
    assert peer.close_finished.is_set()
    assert not peer.network_thread.is_alive()
    assert sum(message["type"] == "stop" for message in peer.sent) <= 1


def test_constructor_is_lazy_and_disconnect_is_idempotent(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Constructor must not resolve secrets or create a thread/peer")

    monkeypatch.setattr(native_client, "Peer", forbidden)
    monkeypatch.setattr(native_client, "_credentials", forbidden)
    monkeypatch.setattr(native_client.threading, "Thread", forbidden)
    client = RemoteRobotClient(RemoteRobotOptions())
    assert not client.is_connected
    with pytest.raises(ConnectionError):
        _ = client.simulated
    client.disconnect()
    client.disconnect()
    with pytest.raises(RuntimeError, match="single-use"):
        client.connect()


def test_connect_waits_for_measured_state_and_repeats_ready(make_client):
    client, scenario = make_client(behavior={"ready_drops": 2})
    assert scenario.peer.ready_count == 3
    assert scenario.peer.channel_label == NATIVE_LABEL
    assert client.is_connected and client.simulated
    observation = client.get_observation()
    assert observation == positions()
    observation["shoulder_pan.pos"] = 999
    assert client.get_observation() == positions()
    assert scenario.peer.actions == []
    assert_closed(client, scenario.peer)


@pytest.mark.parametrize("model", ["so100", "so101", "b601-rs"])
def test_measured_copy_pins_permit_before_background_updates_and_returns_actual(make_client, model):
    client, scenario = make_client(model=model)
    measured = client.get_observation()
    new_position = positions(model)
    new_position["shoulder_pan.pos"] = 20
    on_loop(
        client,
        lambda: scenario.peer.emit(
            "state",
            seq=1,
            position=new_position,
            permit="c" * 32,
        ),
    )
    action = positions(model)
    action["shoulder_pan.pos"] = 10
    actual = client.send_action(action)
    assert actual["shoulder_pan.pos"] == 9
    assert measured["shoulder_pan.pos"] == 0
    assert scenario.peer.actions[0]["permit"] == PERMIT
    assert scenario.peer.actions[0]["action"] == action
    assert client.get_observation() == new_position
    assert actual is not action


@pytest.mark.parametrize("failure", ["missing", "already-used"])
def test_observation_permit_is_required_and_consumed_once(make_client, failure):
    client, scenario = make_client()
    if failure != "missing":
        client.get_observation()
        client.send_action(positions())
    with pytest.raises(RuntimeError, match="get_observation"):
        client.send_action(positions())
    assert len(scenario.peer.actions) == (failure != "missing")
    assert_closed(client, scenario.peer)


def test_explicit_resampling_can_pin_same_fresh_state_but_does_not_renew_its_age(make_client):
    client, scenario = make_client(observation_timeout=0.05)
    sample = client._sample
    for _ in range(2):
        client.get_observation()
        assert client.send_action(positions())["shoulder_pan.pos"] == -1
        assert client._sample is sample
    assert [action["permit"] for action in scenario.peer.actions] == [PERMIT, PERMIT]
    time.sleep(0.06)
    with pytest.raises(TimeoutError):
        client.get_observation()
    assert_closed(client, scenario.peer)


@pytest.mark.parametrize(
    "hello",
    [
        {"joints": list(reversed(arm_model("so101").joints))},
        {"simulated": "true"},
        {"simulated": 1},
        {"simulated": None},
        {"simulated": False, "robot_id": "physical-arm"},
        {"simulated": True, "robot_id": "physical-arm"},
        {"robot_id": ""},
        {"robot_id": 123},
        {"timeout": True},
        {"timeout": 0},
        {"timeout": -1},
        {"video": {"width": 640, "height": 480, "fps": 15}},
    ],
)
def test_bad_handshake_never_grants_ready_or_connects(make_client, hello):
    client, scenario = make_client(connect=False, behavior={"hello": hello})
    with pytest.raises(ConnectionError, match="Invalid native Robot"):
        client.connect()
    assert scenario.peer.ready_count == 0
    assert_closed(client, scenario.peer)


@pytest.mark.parametrize(
    "envelope",
    [
        {"model": "so100"},
        {"units": "degrees"},
        {"session": "A" * 32},
        {"v": True},
    ],
)
def test_wrong_model_units_or_session_never_grants_ready(make_client, envelope):
    client, scenario = make_client(connect=False, behavior={"envelope": envelope})
    with pytest.raises(ConnectionError):
        client.connect()
    assert scenario.peer.ready_count == 0
    assert_closed(client, scenario.peer)


def test_explicit_physical_consent_and_remote_identity_match(make_client):
    client, scenario = make_client(
        allow_physical=True,
        remote_id="calibrated-arm",
        behavior={"hello": {"simulated": False, "robot_id": "calibrated-arm"}},
    )
    assert client.simulated is False
    assert scenario.peer.sent[0]["allow_physical"] is True


def test_remote_identity_mismatch_rejected_before_ready(make_client):
    client, scenario = make_client(connect=False, remote_id="another-arm")
    with pytest.raises(ConnectionError):
        client.connect()
    assert scenario.peer.ready_count == 0
    assert_closed(client, scenario.peer)


@pytest.mark.parametrize(
    "video",
    [
        None,
        {"width": 320, "height": 480, "fps": 15},
        {"width": 640, "height": 240, "fps": 15},
        {"width": 640, "height": 480, "fps": 30},
        {"width": 640.0, "height": 480, "fps": 15},
        {"width": 640, "height": 480, "fps": 15, "extra": 1},
    ],
)
def test_camera_handshake_requires_exact_shape_fps(make_client, video):
    client, scenario = make_client(
        connect=False,
        camera_name="front",
        behavior={"hello": {"video": video}},
    )
    with pytest.raises(ConnectionError):
        client.connect()
    assert scenario.peer.ready_count == 0
    assert scenario.peer.receiver.closed
    assert_closed(client, scenario.peer)


def test_video_waits_first_frame_and_passive_thread_does_not_renew_pin(make_client):
    before = time.monotonic()
    client, scenario = make_client(camera_name="front", behavior={"frame_delay": 0.04})
    assert time.monotonic() - before >= 0.04
    client.get_observation()
    pinned = client._pin
    frames = []
    errors = []

    def passive():
        try:
            assert client.is_connected and client.simulated
            frames.append(client.get_frame())
        except BaseException as exc:
            errors.append(exc)

    reader = threading.Thread(target=passive)
    reader.start()
    reader.join(2)
    assert not reader.is_alive() and not errors
    assert frames == [scenario.peer.receiver.frame]
    assert isinstance(frames[0], av.VideoFrame)
    assert client._pin is pinned
    client.send_action(positions())
    assert scenario.peer.actions[0]["permit"] == PERMIT


def test_passive_frame_without_observation_cannot_authorize_action(make_client):
    client, scenario = make_client(camera_name="front")
    assert client.get_frame() is scenario.peer.receiver.frame
    with pytest.raises(RuntimeError, match="get_observation"):
        client.send_action(positions())
    assert not scenario.peer.actions


@pytest.mark.parametrize("failure", ["stale", "wrong-shape"])
def test_unusable_frame_fails_closed(make_client, failure):
    client, scenario = make_client(camera_name="front", video_timeout=0.05)
    if failure == "stale":
        time.sleep(0.06)
        error = TimeoutError
    else:
        on_loop(
            client,
            lambda: setattr(
                scenario.peer.receiver,
                "frame",
                av.VideoFrame(320, 240, "yuv420p"),
            ),
        )
        error = ValueError
    with pytest.raises(error):
        client.get_frame()
    assert_closed(client, scenario.peer)


@pytest.mark.parametrize("pin", [False, True])
def test_old_observation_or_computation_expires_despite_fresh_background_state(make_client, pin):
    client, scenario = make_client(observation_timeout=0.05)
    if pin:
        client.get_observation()
    time.sleep(0.06)
    if pin:
        on_loop(
            client,
            lambda: scenario.peer.emit(
                "state",
                seq=1,
                position=positions(),
                permit="c" * 32,
            ),
        )
    with pytest.raises(TimeoutError, match="observation expired"):
        if pin:
            client.send_action(positions())
        else:
            client.get_observation()
    assert scenario.peer.actions == []
    assert_closed(client, scenario.peer)


def test_host_timeout_also_caps_local_pinned_sample_age(make_client):
    client, scenario = make_client(behavior={"hello": {"timeout": 0.05}})
    client.get_observation()
    time.sleep(0.06)
    with pytest.raises(TimeoutError):
        client.send_action(positions())
    assert scenario.peer.actions == []


@pytest.mark.parametrize("operation", ["get_observation", "send_action"])
def test_foreign_controller_cannot_rebless_or_submit(make_client, operation):
    client, scenario = make_client()
    client.get_observation()
    errors = []

    def foreign():
        try:
            getattr(client, operation)(*([positions()] if operation == "send_action" else []))
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=foreign)
    thread.start()
    thread.join(2)
    assert not thread.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], RuntimeError)
    assert "one controlling thread" in str(errors[0])
    assert scenario.peer.actions == []
    assert_closed(client, scenario.peer)


def test_foreign_observation_cancels_outstanding_action_instead_of_queuing(make_client):
    dispatched = threading.Event()

    def no_result(peer, data):
        dispatched.set()

    client, scenario = make_client(behavior={"on_action": no_result})
    errors = []

    def controller():
        try:
            client.get_observation()
            client.send_action(positions())
        except BaseException as exc:
            errors.append(exc)

    owner = threading.Thread(target=controller)
    owner.start()
    assert dispatched.wait(1)
    with pytest.raises(RuntimeError, match="one controlling thread"):
        client.get_observation()
    owner.join(2)
    assert not owner.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], ConnectionError)
    assert len(scenario.peer.actions) == 1
    assert_closed(client, scenario.peer)


@pytest.mark.parametrize("changed", [False, True])
def test_repeated_hello_is_idempotent_but_cannot_change_marker(make_client, changed):
    client, scenario = make_client(allow_physical=True)
    client.get_observation()
    pinned = client._pin
    fields = {
        "joints": list(arm_model("so101").joints),
        "simulated": not changed,
        "robot_id": "physical-arm" if changed else "simulated",
        "timeout": 0.4,
        "video": None,
    }
    on_loop(client, lambda: scenario.peer.emit("hello", **fields))
    if changed:
        assert_closed(client, scenario.peer)
    else:
        assert client.is_connected
        assert client._pin is pinned
        assert scenario.peer.ready_count == 1
        client.send_action(positions())


def test_completed_result_duplicates_are_ignored_even_during_next_action(make_client):
    previous = positions()
    previous["shoulder_pan.pos"] = -1

    def duplicate_then_result(peer, data):
        if data["seq"] > 0:
            peer.emit("result", seq=0, action=previous)
        peer.emit("result", seq=data["seq"], action=data["action"])

    client, scenario = make_client(behavior={"on_action": duplicate_then_result})
    for seq in range(3):
        if seq:
            on_loop(
                client,
                lambda seq=seq: scenario.peer.emit(
                    "state",
                    seq=seq,
                    position=positions(),
                    permit=f"{seq:032x}",
                ),
            )
        client.get_observation()
        assert client.send_action(positions()) == positions()
    assert client.is_connected
    assert [action["seq"] for action in scenario.peer.actions] == [0, 1, 2]


@pytest.mark.parametrize("failure", ["unknown", "invalid-action", "session", "units"])
def test_bad_result_fails_closed_without_echo_or_retry(make_client, failure):
    def bad_result(peer, data):
        result = json.loads(
            native_message(
                "so101",
                SESSION,
                "result",
                seq=data["seq"],
                action=data["action"],
            )
        )
        if failure == "unknown":
            result["seq"] += 1
        elif failure == "invalid-action":
            result["action"]["shoulder_pan.pos"] = 1000
        elif failure == "session":
            result["session"] = "d" * 32
        else:
            result["units"] = "degrees"
        peer.on_message(json.dumps(result))

    client, scenario = make_client(behavior={"on_action": bad_result})
    client.get_observation()
    with pytest.raises(ConnectionError):
        client.send_action(positions())
    assert len(scenario.peer.actions) == 1
    assert_closed(client, scenario.peer)


def test_unsolicited_result_is_terminal(make_client):
    client, scenario = make_client()
    on_loop(client, lambda: scenario.peer.emit("result", seq=0, action=positions()))
    assert_closed(client, scenario.peer)


def test_invalid_old_duplicate_is_not_ignored(make_client):
    client, scenario = make_client()
    client.get_observation()
    client.send_action(positions())
    on_loop(client, lambda: scenario.peer.emit("result", seq=0, action={}))
    assert_closed(client, scenario.peer)


@pytest.mark.parametrize("failure", ["missing-joint", "out-of-range", "boolean", "nan"])
def test_invalid_sdk_action_never_enters_transport(make_client, failure):
    client, scenario = make_client()
    action = positions()
    if failure == "missing-joint":
        del action["shoulder_pan.pos"]
    else:
        action["shoulder_pan.pos"] = {
            "out-of-range": 101,
            "boolean": True,
            "nan": float("nan"),
        }[failure]
    client.get_observation()
    with pytest.raises(ValueError):
        client.send_action(action)
    assert scenario.peer.actions == []
    assert_closed(client, scenario.peer)


def test_reordered_state_does_not_refresh_local_deadline_or_replace_measurement(make_client):
    client, scenario = make_client(observation_timeout=0.05)
    sample = client._sample
    time.sleep(0.06)
    on_loop(
        client,
        lambda: scenario.peer.emit(
            "state",
            seq=0,
            position=positions(),
            permit="c" * 32,
        ),
    )
    assert client._sample is sample
    with pytest.raises(TimeoutError):
        client.get_observation()


@pytest.mark.parametrize(
    "message",
    [
        "not-json",
        b"binary",
        "x" * 16385,
        native_message("so101", "d" * 32, "state", seq=1, position=positions(), permit=PERMIT),
        native_message("so101", SESSION, "state", seq=True, position=positions(), permit=PERMIT),
        native_message("so101", SESSION, "state", seq=1, position={}, permit=PERMIT),
        native_message("so101", SESSION, "state", seq=1, position=positions(), permit="bad"),
    ],
)
def test_invalid_state_or_epoch_change_closes_background_connection(make_client, message):
    client, scenario = make_client()
    on_loop(client, lambda: scenario.peer.on_message(message))
    assert_closed(client, scenario.peer)


def test_b601_measured_feedback_tolerance_is_not_action_tolerance(make_client):
    client, scenario = make_client(model="b601-rs")
    measured = positions("b601-rs")
    measured["shoulder_pan.pos"] = 145.2
    on_loop(client, lambda: scenario.peer.emit("state", seq=1, position=measured, permit="c" * 32))
    assert client.get_observation() == measured
    with pytest.raises(ValueError):
        client.send_action(measured)
    assert not scenario.peer.actions


@pytest.mark.parametrize("failure", ["no-state", "no-frame", "partial-error"])
def test_partial_startup_owns_cleanup_before_connect_raises(make_client, failure):
    behavior = {failure.replace("-", "_"): True}
    client, scenario = make_client(
        connect=False,
        connect_timeout=0.08,
        behavior=behavior,
        **({"camera_name": "front"} if failure != "no-state" else {}),
    )
    with pytest.raises((TimeoutError, ConnectionError)) as rejected:
        client.connect()
    assert TOKEN not in str(rejected.value)
    assert_closed(client, scenario.peer)
    if scenario.peer.receiver is not None:
        assert scenario.peer.receiver.closed


def test_disconnect_interrupts_partial_connect_and_joins_all_work(make_client):
    client, scenario = make_client(connect=False, behavior={"no_state": True})
    errors = []

    def connecting():
        try:
            client.connect()
        except BaseException as exc:
            errors.append(exc)

    caller = threading.Thread(target=connecting)
    caller.start()
    deadline = time.monotonic() + 1
    while not hasattr(scenario, "peer"):
        assert time.monotonic() < deadline
        time.sleep(0.002)
    client.disconnect()
    caller.join(2)
    assert not caller.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], ConnectionError)
    assert_closed(client, scenario.peer)


def test_background_peer_failure_closes_without_further_sdk_calls(make_client):
    client, scenario = make_client()
    on_loop(client, scenario.peer.closed.set)
    assert client._closed.wait(1)
    assert_closed(client, scenario.peer)


def test_failed_peer_constructor_still_closes_receiver(make_client, monkeypatch):
    client, _ = make_client(connect=False, camera_name="front")
    receivers = []
    original = native_client._make_video_receiver

    def receiver(options):
        value = original(options)
        receivers.append(value)
        return value

    def broken(*args, **kwargs):
        raise RuntimeError(TOKEN)

    monkeypatch.setattr(native_client, "_make_video_receiver", receiver)
    monkeypatch.setattr(native_client, "Peer", broken)
    with pytest.raises(ConnectionError) as rejected:
        client.connect()
    assert TOKEN not in str(rejected.value)
    assert receivers[0].closed
    assert not client._thread.is_alive()


def test_thread_start_failure_does_not_wait_for_nonexistent_cleanup(make_client, monkeypatch):
    client, _ = make_client(connect=False)

    def broken(self):
        raise RuntimeError("thread start failure")

    monkeypatch.setattr(threading.Thread, "start", broken)
    with pytest.raises(RuntimeError, match="thread start failure"):
        client.connect()
    client.disconnect()


@pytest.mark.parametrize("mode", ["timeout", "congested"])
def test_uncertain_send_closes_and_never_replays(make_client, mode):
    client, scenario = make_client(
        request_timeout=0.05,
        behavior={"no_result": mode == "timeout", "congested": mode == "congested"},
    )
    client.get_observation()
    before = time.monotonic()
    with pytest.raises((TimeoutError, ConnectionError)):
        client.send_action(positions())
    if mode == "timeout":
        assert time.monotonic() - before >= 0.045
    assert len(scenario.peer.actions) == 1
    assert_closed(client, scenario.peer)
    time.sleep(0.06)
    assert len(scenario.peer.actions) == 1
    with pytest.raises((RuntimeError, ConnectionError)):
        client.send_action(positions())


def test_delayed_network_dispatch_cannot_write_after_sync_timeout(make_client):
    client, scenario = make_client(request_timeout=0.05)
    client.get_observation()
    entered = threading.Event()

    def stall_network():
        entered.set()
        time.sleep(0.1)

    client._loop.call_soon_threadsafe(stall_network)
    assert entered.wait(1)
    with pytest.raises(TimeoutError):
        client.send_action(positions())
    assert scenario.peer.actions == []
    assert_closed(client, scenario.peer)


def test_keyboard_interrupt_of_sdk_wait_is_terminal(make_client, monkeypatch):
    client, scenario = make_client(behavior={"no_result": True})
    client.get_observation()
    original = native_client.concurrent.futures.Future.result

    def interrupt(future, timeout=None):
        if timeout is not None and future is not client._connected_result:
            raise KeyboardInterrupt
        return original(future, timeout)

    monkeypatch.setattr(native_client.concurrent.futures.Future, "result", interrupt)
    with pytest.raises(KeyboardInterrupt):
        client.send_action(positions())
    assert_closed(client, scenario.peer)
    assert len(scenario.peer.actions) <= 1


def test_disconnect_waits_for_owned_close_even_if_network_task_cancelled(make_client):
    gate = threading.Event()
    client, scenario = make_client(behavior={"close_gate": gate})
    disconnected = threading.Event()

    def disconnect():
        client.disconnect()
        disconnected.set()

    caller = threading.Thread(target=disconnect)
    caller.start()
    try:
        assert scenario.peer.close_started.wait(1)
        assert not disconnected.is_set()
        assert not client.is_connected

        def cancel_owner():
            for task in asyncio.all_tasks():
                if task.get_coro().__qualname__ == "RemoteRobotClient._run":
                    task.cancel()

        on_loop(client, cancel_owner)
        time.sleep(0.02)
        assert not disconnected.is_set()
    finally:
        gate.set()
        caller.join(2)
    assert disconnected.is_set()
    assert_closed(client, scenario.peer)


@pytest.mark.parametrize("token", ["", "short", "x" * 15, "x" * 16 + "\n", "é" * 16])
def test_runtime_token_validation_never_exposes_secret(make_client, monkeypatch, token):
    client, scenario = make_client(connect=False)
    monkeypatch.setenv("LEROBOT_WEBRTC_TOKEN", token)
    with pytest.raises(ValueError, match="printable ASCII") as rejected:
        client.connect()
    if token:
        assert token not in str(rejected.value)
    assert not hasattr(scenario, "peer")
    assert client._thread is None


def test_credentials_resolved_only_at_connect_and_debug_suppressed(make_client, monkeypatch):
    client, scenario = make_client(
        connect=False,
        token_env="OTHER_TOKEN",
        stun=["stun:example.com:3478"],
        turn="turns:relay.example.com:5349?transport=tcp",
        turn_username_env="OTHER_USER",
        turn_password_env="OTHER_PASSWORD",
    )
    monkeypatch.setenv("OTHER_TOKEN", "resolved-token-not-in-config")
    monkeypatch.setenv("OTHER_USER", "resolved-turn-user")
    monkeypatch.setenv("OTHER_PASSWORD", "resolved-turn-password")
    for name in ("aiortc", "aioice", "aiohttp"):
        monkeypatch.setattr(logging.getLogger(name), "level", logging.DEBUG)
    client.connect()
    assert scenario.peer.token == "resolved-token-not-in-config"
    assert scenario.peer.ice_servers[0].urls == "stun:example.com:3478"
    turn = scenario.peer.ice_servers[1]
    assert turn.username == "resolved-turn-user"
    assert turn.credential == "resolved-turn-password"
    for secret in (scenario.peer.token, turn.username, turn.credential):
        assert secret not in repr(client.options)
        assert secret not in repr(client._settings)
    for name in ("aiortc", "aioice", "aiohttp"):
        assert not logging.getLogger(name).isEnabledFor(logging.DEBUG)


def test_missing_turn_credentials_fail_before_thread_start(make_client, monkeypatch):
    client, scenario = make_client(connect=False, turn="turn:relay.example.com")
    monkeypatch.delenv("LEROBOT_TURN_USERNAME", raising=False)
    monkeypatch.delenv("LEROBOT_TURN_PASSWORD", raising=False)
    with pytest.raises(ValueError, match="TURN credentials"):
        client.connect()
    assert not hasattr(scenario, "peer")
    assert client._thread is None


@pytest.mark.parametrize("model", ["so100", "so101", "b601-rs"])
@pytest.mark.parametrize("video", [False, True])
async def test_real_native_host_measured_state_actual_result_and_stop(
    signaling_server,
    monkeypatch,
    model,
    video,
):
    from lerobot_webrtc.arms import AsyncArm, SimulatedArm
    from lerobot_webrtc.native_host import run_robot_host
    from lerobot_webrtc.peer import Peer
    from lerobot_webrtc.video_sources import SyntheticVideoTrack

    monkeypatch.setenv("LEROBOT_WEBRTC_TOKEN", TOKEN)
    url, _ = signaling_server
    device = SimulatedArm(delta=2, model=model)
    options = RemoteRobotOptions(
        model=model,
        signaling=url,
        room="native-client",
        camera_name="front" if video else None,
        video_width=320,
        video_height=240,
        video_fps=15,
    )
    source = SyntheticVideoTrack(options.video_settings()) if video else None
    peer = Peer(
        "follower",
        url,
        "native-client",
        TOKEN,
        channel_label=NATIVE_LABEL,
        video_source=source,
    )
    host = asyncio.create_task(
        run_robot_host(
            AsyncArm(device, "follower"),
            peer,
            model,
            30,
            0.3,
            None,
        )
    )
    client = RemoteRobotClient(options)

    def control():
        try:
            client.connect()
            assert client.is_connected and client.simulated
            measured = client.get_observation()
            if video:
                frame = client.get_frame()
                assert isinstance(frame, av.VideoFrame)
                assert (frame.width, frame.height) == (320, 240)
                assert client._receiver.frames_received > 0
            target = measured.copy()
            target["shoulder_pan.pos"] += 10
            result = client.send_action(target)
            assert result["shoulder_pan.pos"] == measured["shoulder_pan.pos"] + 2
            assert result != target
            # Let periodic telemetry arrive; the caller still has to sample it.
            time.sleep(0.06)
            assert client.get_observation()["shoulder_pan.pos"] == result["shoulder_pan.pos"]
        finally:
            client.disconnect()

    try:
        await asyncio.wait_for(asyncio.to_thread(control), 10)
        outcome = await asyncio.wait_for(asyncio.gather(host, return_exceptions=True), 3)
        assert outcome[0] is None or isinstance(outcome[0], ConnectionError)
        assert device.applied == 1
        assert device.held and not device.connected
        assert peer.pc.connectionState == "closed"
        assert not client._thread.is_alive()
    finally:
        host.cancel()
        await asyncio.gather(host, return_exceptions=True)
        await asyncio.to_thread(client.disconnect)
