"""Real SDK contracts with a fake native client, never a motor or camera device."""

import importlib
import os
import subprocess
import sys
import textwrap
import threading
from dataclasses import asdict
from importlib.metadata import PackageNotFoundError, version
from types import ModuleType, SimpleNamespace

import pytest

from lerobot_webrtc.models import arm_model


@pytest.fixture(
    params=[
        pytest.param("0.6.1", marks=pytest.mark.hardware_api, id="sdk-0.6.1"),
        pytest.param("0.6.2", marks=pytest.mark.b601_api, id="sdk-b601-source"),
    ]
)
def sdk(request, monkeypatch, tmp_path):
    # All SDK imports are lazy: base installations can collect/deselect this file.
    try:
        installed = version("lerobot")
    except PackageNotFoundError:
        pytest.fail("Selected native plugin SDK tests require the corresponding pinned LeRobot SDK")
    assert installed == request.param, (
        f"Selected native plugin tests need LeRobot {request.param}, found {installed}; "
        "run each SDK marker in its own pinned environment"
    )
    from lerobot.robots.config import RobotConfig
    from lerobot.robots.robot import Robot
    from lerobot.robots.utils import make_robot_from_config

    from lerobot_robot_webrtc import WebRTCRobot, WebRTCRobotConfig

    robot_module = importlib.import_module("lerobot.robots.robot")
    monkeypatch.setattr(robot_module, "HF_LEROBOT_CALIBRATION", tmp_path / "sdk-cache")
    return SimpleNamespace(
        Config=WebRTCRobotConfig,
        Robot=WebRTCRobot,
        RobotConfig=RobotConfig,
        RobotBase=Robot,
        make=make_robot_from_config,
        calibration_dir=tmp_path / "calibration",
    )


@pytest.fixture
def fake_clients(monkeypatch):
    control = SimpleNamespace(
        instances=[],
        events=[],
        connect_error=None,
        connect_ready=True,
        disconnect_error=None,
        frame=None,
        frame_error=None,
        observation_error=None,
        send_error=None,
    )

    class FakeRemoteClient:
        def __init__(self, options):
            self.options = options
            self.is_connected = False
            self.simulated = True
            self.position = {
                joint: float(index) for index, joint in enumerate(arm_model(options.model).joints)
            }
            self.applied = dict.fromkeys(self.position, 1.0)
            self.requested = None
            control.instances.append(self)
            control.events.append(("construct", self))

        def connect(self):
            control.events.append(("connect", self))
            self.is_connected = control.connect_ready
            if control.connect_error is not None:
                raise control.connect_error

        def disconnect(self):
            control.events.append(("disconnect", self))
            self.is_connected = False
            if control.disconnect_error is not None:
                raise control.disconnect_error

        def get_observation(self):
            assert self.is_connected
            control.events.append(("observation", self))
            if control.observation_error is not None:
                raise control.observation_error
            return self.position

        def get_frame(self):
            assert self.is_connected
            control.events.append(("frame", self))
            if control.frame_error is not None:
                raise control.frame_error
            assert control.frame is not None, "No synthetic fallback frame is permitted"
            return control.frame

        def send_action(self, action):
            assert self.is_connected
            control.events.append(("action", self))
            if control.send_error is not None:
                raise control.send_error
            self.requested = action.copy()
            return self.applied

    module = ModuleType("lerobot_webrtc.native_client")
    module.RemoteRobotClient = FakeRemoteClient
    monkeypatch.setitem(sys.modules, module.__name__, module)
    return control


def test_real_sdk_registration_factory_and_inherited_identity(sdk, fake_clients):
    from lerobot_webrtc.native_options import RemoteRobotOptions

    config = sdk.Config(id="remote_proxy", calibration_dir=sdk.calibration_dir)
    assert sdk.RobotConfig.get_choice_class("webrtc") is sdk.Config
    assert config.type == "webrtc"
    assert isinstance(config, RemoteRobotOptions)
    robot = sdk.make(config)
    assert type(robot) is sdk.Robot
    assert isinstance(robot, sdk.RobotBase)
    assert robot.name == robot.robot_type == "webrtc"
    assert robot.config_class is sdk.Config
    assert robot.config is config
    assert robot.id == "remote_proxy"
    assert robot.calibration_dir == sdk.calibration_dir
    assert robot.calibration_fpath == sdk.calibration_dir / "remote_proxy.json"
    assert robot.calibration_dir.is_dir()  # The SDK base's only construction I/O.
    assert not robot.is_connected
    assert robot.is_calibrated
    assert robot.cameras == {}
    assert not fake_clients.instances
    with pytest.raises(TypeError):
        sdk.Config("not-keyword-only")


@pytest.mark.parametrize("change", ["model", "stun"])
def test_feature_and_connection_config_cannot_drift_before_connect(sdk, fake_clients, change):
    config = sdk.Config(calibration_dir=sdk.calibration_dir)
    robot = sdk.make(config)
    if change == "model":
        config.model = "b601-rs"
    else:
        config.stun.append("stun:example.com:3478")
    with pytest.raises(ValueError, match="new WebRTCRobot"):
        robot.connect()
    assert not fake_clients.instances


def test_config_calls_both_base_validators(sdk, monkeypatch):
    from lerobot_webrtc.native_options import RemoteRobotOptions

    calls = []
    robot_validate = sdk.RobotConfig.__post_init__
    remote_validate = RemoteRobotOptions.__post_init__

    def validate_robot(config):
        calls.append("sdk")
        robot_validate(config)

    def validate_remote(config):
        calls.append("remote")
        remote_validate(config)

    monkeypatch.setattr(sdk.RobotConfig, "__post_init__", validate_robot)
    monkeypatch.setattr(RemoteRobotOptions, "__post_init__", validate_remote)
    sdk.Config(calibration_dir=sdk.calibration_dir)
    assert calls == ["sdk", "remote"]
    with pytest.raises(ValueError, match="environment variable"):
        sdk.Config(token_env="raw secret is not an environment name")
    with pytest.raises(ValueError, match="Unsupported arm model"):
        sdk.Config(model="unknown")


def test_config_serializes_only_credential_names_without_resolving_secrets(sdk, monkeypatch):
    credentials = {
        "PLUGIN_TEST_TOKEN": "resolved-token-should-never-be-copied",
        "PLUGIN_TEST_TURN_USER": "resolved-turn-user-should-never-be-copied",
        "PLUGIN_TEST_TURN_PASSWORD": "resolved-turn-password-should-never-be-copied",
    }
    for name, secret in credentials.items():
        monkeypatch.setenv(name, secret)
    environ_getitem = type(os.environ).__getitem__

    def forbid_resolution(environ, key):
        assert key not in credentials, "Config must never resolve runtime credentials"
        return environ_getitem(environ, key)

    monkeypatch.setattr(type(os.environ), "__getitem__", forbid_resolution)
    config = sdk.Config(
        token_env="PLUGIN_TEST_TOKEN",
        turn="turn:localhost:3478",
        turn_username_env="PLUGIN_TEST_TURN_USER",
        turn_password_env="PLUGIN_TEST_TURN_PASSWORD",
    )
    serialized = asdict(config)
    assert serialized["token_env"] == "PLUGIN_TEST_TOKEN"
    assert serialized["turn_username_env"] == "PLUGIN_TEST_TURN_USER"
    assert serialized["turn_password_env"] == "PLUGIN_TEST_TURN_PASSWORD"
    for secret in credentials.values():
        assert secret not in repr(config)
        assert secret not in repr(serialized)
    for name in ("token", "turn_username", "turn_password"):
        with pytest.raises(TypeError, match="unexpected keyword"):
            sdk.Config(**{name: "no-raw-secrets"})


@pytest.mark.parametrize("model", ["so100", "so101", "b601-rs"])
@pytest.mark.parametrize("camera_name", [None, "front"])
def test_preconnect_feature_schema_and_real_sdk_converters(sdk, fake_clients, model, camera_name):
    import numpy as np
    from lerobot.configs import FeatureType
    from lerobot.utils.feature_utils import (
        build_dataset_frame,
        dataset_to_policy_features,
        hw_to_dataset_features,
    )

    config = sdk.Config(
        model=model,
        camera_name=camera_name,
        video_width=32,
        video_height=24,
        video_fps=15,
        calibration_dir=sdk.calibration_dir,
    )
    robot = sdk.make(config)
    joints = arm_model(model).joints
    expected = dict.fromkeys(joints, float)
    assert robot.action_features == expected
    assert list(robot.action_features) == list(joints)
    assert all(value is float for value in robot.action_features.values())
    if camera_name is not None:
        expected[camera_name] = (24, 32, 3)
        camera = robot.cameras[camera_name]
        assert (camera.height, camera.width, camera.fps) == (24, 32, 15)
        assert not camera.is_connected
    else:
        assert robot.cameras == {}
    assert robot.observation_features == expected
    robot.observation_features.clear()
    robot.action_features.clear()
    assert robot.observation_features == expected
    assert list(robot.action_features) == list(joints)
    assert not fake_clients.instances

    features = hw_to_dataset_features(robot.action_features, "action")
    features.update(hw_to_dataset_features(robot.observation_features, "observation"))
    assert features["action"]["names"] == list(joints)
    assert features["observation.state"]["names"] == list(joints)
    assert features["observation.state"]["shape"] == (len(joints),)
    policy = dataset_to_policy_features(features)
    assert policy["action"].type == FeatureType.ACTION
    assert policy["observation.state"].type == FeatureType.STATE
    values = {joint: float(index) for index, joint in enumerate(joints)}
    if camera_name is not None:
        assert features["observation.images.front"]["shape"] == (24, 32, 3)
        assert policy["observation.images.front"].shape == (3, 24, 32)
        assert policy["observation.images.front"].type == FeatureType.VISUAL
        values[camera_name] = np.zeros((24, 32, 3), dtype=np.uint8)
    else:
        assert set(features) == {"action", "observation.state"}
    frame = build_dataset_frame(features, values, "observation")
    assert frame["observation.state"].dtype == np.float32
    np.testing.assert_array_equal(frame["observation.state"], np.arange(len(joints)))
    if camera_name is not None:
        assert frame["observation.images.front"] is values[camera_name]

    robot.connect()
    try:
        assert robot.observation_features == expected
        assert robot.action_features == dict.fromkeys(joints, float)
    finally:
        robot.disconnect()
    assert robot.observation_features == expected


def test_measured_observation_rgb_copy_and_actual_applied_target(sdk, fake_clients):
    import numpy as np

    config = sdk.Config(
        camera_name="front",
        video_width=32,
        video_height=24,
        calibration_dir=sdk.calibration_dir,
    )
    pixels = np.full((24, 32, 3), (1, 31, 251), dtype=np.uint8)
    conversions = []

    def convert(*, format):
        conversions.append((format, threading.get_ident()))
        fake_clients.events.append(("convert", None))
        return pixels

    fake_clients.frame = SimpleNamespace(to_ndarray=convert)
    robot = sdk.make(config)
    robot.connect()
    client = fake_clients.instances[-1]
    try:
        observation = robot.get_observation()
        assert [event for event, _ in fake_clients.events[-3:]] == [
            "observation",
            "frame",
            "convert",
        ]
        assert {joint: observation[joint] for joint in client.position} == client.position
        assert conversions == [("rgb24", threading.get_ident())]
        np.testing.assert_array_equal(observation["front"], pixels)
        assert not np.shares_memory(observation["front"], pixels)
        observation["front"][:] = 0
        assert pixels[0, 0].tolist() == [1, 31, 251]
        observation[next(iter(client.position))] = -999.0
        assert -999.0 not in client.position.values()

        requested = dict.fromkeys(client.position, 9.0)
        actual = robot.send_action(requested)
        assert client.requested == requested
        assert actual == client.applied
        assert actual != requested
        assert actual != client.position
        actual[next(iter(actual))] = -999.0
        assert -999.0 not in client.applied.values()

        camera = robot.cameras["front"]
        assert camera.is_connected
        count = len(fake_clients.events)
        np.testing.assert_array_equal(camera.read(), pixels)
        np.testing.assert_array_equal(camera.async_read(timeout_ms=10), pixels)
        assert [event for event, _ in fake_clients.events[count:]] == [
            "frame",
            "convert",
            "frame",
            "convert",
        ]  # Camera reads do not fetch observations or refresh the action permit.
        assert len(fake_clients.instances) == 1
    finally:
        robot.disconnect()
    assert not camera.is_connected


def test_data_only_observation_has_no_image_or_frame_request(sdk, fake_clients):
    robot = sdk.make(sdk.Config(calibration_dir=sdk.calibration_dir))
    robot.connect(calibrate=False)
    try:
        client = fake_clients.instances[-1]
        observation = robot.get_observation()
        assert observation == client.position
        assert observation is not client.position
        assert robot.cameras == {}
        assert not any(event == "frame" for event, _ in fake_clients.events)
    finally:
        robot.disconnect()


def test_reads_and_sends_never_implicitly_connect_or_resume(sdk, fake_clients):
    from lerobot.utils.errors import DeviceAlreadyConnectedError, DeviceNotConnectedError

    robot = sdk.make(sdk.Config(camera_name="front", calibration_dir=sdk.calibration_dir))
    action = dict.fromkeys(arm_model("so101").joints, 0.0)
    for operation in (
        robot.get_observation,
        lambda: robot.send_action(action),
        robot.cameras["front"].read,
        robot.cameras["front"].async_read,
    ):
        with pytest.raises(DeviceNotConnectedError, match="connect"):
            operation()
    assert not fake_clients.instances
    robot.connect()
    client = fake_clients.instances[-1]
    with pytest.raises(DeviceAlreadyConnectedError):
        robot.connect()
    client.is_connected = False  # A terminal network failure can leave an owned thread.
    events = fake_clients.events.copy()
    for operation in (robot.get_observation, lambda: robot.send_action(action)):
        with pytest.raises(DeviceNotConnectedError):
            operation()
    assert fake_clients.events == events
    robot.disconnect()
    assert fake_clients.events[-1] == ("disconnect", client)
    robot.disconnect()
    assert fake_clients.events[-1] == ("disconnect", client)
    assert len(fake_clients.instances) == 1
    robot.connect()  # Only an explicit connect creates a new transport session.
    assert len(fake_clients.instances) == 2
    assert fake_clients.instances[-1] is not client
    robot.disconnect()


@pytest.mark.parametrize("ready", [False, True])
def test_partial_connect_failure_is_cleaned_before_sdk_caller_finally(sdk, fake_clients, ready):
    robot = sdk.make(sdk.Config(calibration_dir=sdk.calibration_dir))
    error = ConnectionError("native host rejected the session")
    fake_clients.connect_error = error
    fake_clients.connect_ready = ready
    with pytest.raises(ConnectionError, match="host rejected") as raised:
        robot.connect()
    assert raised.value is error
    client = fake_clients.instances[-1]
    assert not robot.is_connected
    assert fake_clients.events[-1] == ("disconnect", client)
    events = fake_clients.events.copy()
    robot.disconnect()
    assert fake_clients.events == events
    fake_clients.connect_error = None
    fake_clients.connect_ready = True
    robot.connect()
    assert len(fake_clients.instances) == 2
    robot.disconnect()


def test_client_must_be_ready_and_sdk_startup_errors_also_cleanup(sdk, fake_clients, monkeypatch):
    robot = sdk.make(sdk.Config(calibration_dir=sdk.calibration_dir))
    fake_clients.connect_ready = False
    with pytest.raises(ConnectionError, match="did not become ready"):
        robot.connect()
    assert fake_clients.events[-1][0] == "disconnect"
    fake_clients.connect_ready = True

    def fail_configuration():
        raise RuntimeError("SDK startup hook failed")

    monkeypatch.setattr(robot, "configure", fail_configuration)
    with pytest.raises(RuntimeError, match="SDK startup hook"):
        robot.connect()
    assert not robot.is_connected
    assert fake_clients.events[-1][0] == "disconnect"
    before = fake_clients.events.copy()
    robot.disconnect()
    assert fake_clients.events == before


def test_explicit_reconnect_joins_terminal_client_before_creating_new_one(sdk, fake_clients):
    robot = sdk.make(sdk.Config(calibration_dir=sdk.calibration_dir))
    robot.connect()
    previous = fake_clients.instances[-1]
    previous.is_connected = False
    robot.connect()
    current = fake_clients.instances[-1]
    assert fake_clients.events[-3:] == [
        ("disconnect", previous),
        ("construct", current),
        ("connect", current),
    ]
    robot.disconnect()


def test_failed_cleanup_keeps_ownership_and_blocks_replacement(sdk, fake_clients):
    robot = sdk.make(sdk.Config(calibration_dir=sdk.calibration_dir))
    robot.connect()
    client = fake_clients.instances[-1]
    fake_clients.disconnect_error = RuntimeError("join not finished")
    with pytest.raises(RuntimeError, match="join not finished"):
        robot.disconnect()
    with pytest.raises(RuntimeError, match="join not finished"):
        robot.connect()
    assert fake_clients.instances == [client]
    fake_clients.disconnect_error = None
    robot.connect()
    assert len(fake_clients.instances) == 2
    assert fake_clients.events[-3] == ("disconnect", client)
    robot.disconnect()


def test_calibration_is_host_local_and_configure_never_mutates_hardware(sdk, fake_clients):
    robot = sdk.make(sdk.Config(calibration_dir=sdk.calibration_dir))
    assert robot.is_calibrated
    with pytest.raises(RuntimeError, match="calibrate hardware locally on host"):
        robot.calibrate()
    robot.configure()
    assert not fake_clients.instances
    robot.connect(calibrate=True)
    try:
        before = fake_clients.events.copy()
        with pytest.raises(RuntimeError, match="calibrate hardware locally on host"):
            robot.calibrate()
        robot.configure()
        assert fake_clients.events == before
    finally:
        robot.disconnect()


@pytest.mark.parametrize("failure", ["observation", "frame", "shape", "action"])
def test_transport_and_image_errors_propagate_without_fallback(sdk, fake_clients, failure):
    import numpy as np

    config = sdk.Config(
        camera_name="front",
        video_width=32,
        video_height=24,
        calibration_dir=sdk.calibration_dir,
    )
    robot = sdk.make(config)
    robot.connect()
    error = ConnectionError("native data unavailable")
    try:
        if failure == "observation":
            fake_clients.observation_error = error
        elif failure == "frame":
            fake_clients.frame_error = error
        elif failure == "shape":
            fake_clients.frame = SimpleNamespace(
                to_ndarray=lambda **_: np.zeros((16, 16, 3), dtype=np.uint8)
            )
        else:
            fake_clients.send_error = error
        operation = (
            (lambda: robot.send_action(dict.fromkeys(robot.action_features, 9.0)))
            if failure == "action"
            else robot.get_observation
        )
        if failure == "shape":
            with pytest.raises(ValueError, match="configured camera shape"):
                operation()
        else:
            with pytest.raises(ConnectionError) as raised:
                operation()
            assert raised.value is error
        assert len(fake_clients.instances) == 1
    finally:
        robot.disconnect()


def test_import_and_construction_create_no_native_client_network_or_threads(sdk, tmp_path):
    script = textwrap.dedent(
        """
        import asyncio
        import importlib.abc
        import socket
        import sys
        import threading
        from pathlib import Path

        # Isolate plugin behavior from the SDK's own import-time initialization.
        from lerobot.robots.robot import Robot

        def forbidden(*args, **kwargs):
            raise AssertionError('Plugin imports/construction must not perform transport I/O')

        class RejectClientImport(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname == 'lerobot_webrtc.native_client':
                    raise AssertionError('Client must be imported lazily on explicit connect')

        sys.meta_path.insert(0, RejectClientImport())
        socket.socket.connect = forbidden
        socket.create_connection = forbidden
        threading.Thread.start = forbidden
        asyncio.new_event_loop = forbidden

        from lerobot_robot_webrtc import WebRTCRobot, WebRTCRobotConfig

        config = WebRTCRobotConfig(calibration_dir=Path(sys.argv[1]), camera_name='front')
        robot = WebRTCRobot(config)
        assert robot.config is config
        assert not robot.is_connected
        assert 'front' in robot.observation_features
        assert 'lerobot_webrtc.native_client' not in sys.modules
        robot.disconnect()
        """
    )
    completed = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path / "isolated-sdk-cache")],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
