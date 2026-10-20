"""Acceptance of the installed native extra, not replacement SDK control loops.

Only the local leader input and interactive keyboard/audio helpers are fake.
The SDK factory, identity processors, frame builders, dataset writer and native
WebRTC RPC run unchanged. All SDK imports stay lazy for SDK-free collection.
"""

import asyncio
import os
import subprocess
import sys
from dataclasses import asdict
from importlib.metadata import version
from pathlib import Path
from types import SimpleNamespace

import pytest

from .conftest import TOKEN

pytestmark = pytest.mark.native_api


@pytest.fixture
def native_sdk_env(monkeypatch, tmp_path):
    # Set these before importing any SDK module, including in the CLI subprocess.
    for key in ("HF_HUB_OFFLINE", "HF_DATASETS_OFFLINE", "HF_HUB_DISABLE_TELEMETRY"):
        monkeypatch.setenv(key, "1")
    monkeypatch.setenv("HF_HOME", str(tmp_path / "hf"))
    monkeypatch.setenv("LEROBOT_WEBRTC_TOKEN", TOKEN)
    assert version("lerobot") == "0.6.1", "Run with the prepared, pinned native-extra environment"

    # ICE must stay on the test-owned localhost network, with no STUN/TURN service.
    import aioice.ice

    monkeypatch.setattr(aioice.ice, "get_host_addresses", lambda **_: ["127.0.0.1"])


def _leader(config, trace):
    from lerobot.teleoperators import Teleoperator

    from lerobot_webrtc.models import arm_model

    class SyntheticLeader(Teleoperator):
        config_class = type(config)
        name = "synthetic_native_workflow_leader"

        def __init__(self):
            self.connected = False
            super().__init__(config)
            self.request = dict.fromkeys(arm_model("so101").joints, 0.0)
            self.request["shoulder_pan.pos"] = 20.0

        @property
        def action_features(self):
            return dict.fromkeys(self.request, float)

        @property
        def feedback_features(self):
            return {}

        @property
        def is_connected(self):
            return self.connected

        @property
        def is_calibrated(self):
            return True

        def connect(self, calibrate=True):
            trace.events.append("teleop.connect")
            self.connected = True

        def calibrate(self):
            pass

        def configure(self):
            pass

        def get_action(self):
            assert self.connected
            # Even display_data=False must sample measured state before input.
            assert trace.events[-1] == "observation"
            trace.events.append("teleop.action")
            return self.request.copy()

        def send_feedback(self, feedback):
            pytest.fail("The SO workflow must not send synthetic force feedback")

        def disconnect(self):
            trace.events.append("teleop.disconnect")
            self.connected = False

    return SyntheticLeader()


def _observe_real_robot(monkeypatch):
    """Pass-through spies only; the SDK's real Robot factory is never patched."""
    from lerobot_robot_webrtc import WebRTCRobot

    trace = SimpleNamespace(
        events=[],
        observations=[],
        requests=[],
        results=[],
        robot=None,
        client=None,
    )
    connect = WebRTCRobot.connect
    observe = WebRTCRobot.get_observation
    send = WebRTCRobot.send_action

    def connected(robot, *args, **kwargs):
        assert type(robot) is WebRTCRobot
        trace.robot = robot
        trace.events.append("robot.connect")
        connect(robot, *args, **kwargs)
        trace.client = robot._client

    def observed(robot):
        value = observe(robot)
        trace.observations.append(value.copy())
        trace.events.append("observation")
        return value

    def sent(robot, action):
        assert trace.events[-1] == "teleop.action"
        trace.requests.append(action.copy())
        result = send(robot, action)
        trace.results.append(result.copy())
        trace.events.append("robot.action")
        return result

    monkeypatch.setattr(WebRTCRobot, "connect", connected)
    monkeypatch.setattr(WebRTCRobot, "get_observation", observed)
    monkeypatch.setattr(WebRTCRobot, "send_action", sent)
    return trace


def _workflow(monkeypatch, tmp_path, signaling_server, script, *, camera):
    from lerobot.teleoperators.so_leader import SO101LeaderConfig

    from lerobot_robot_webrtc import WebRTCRobotConfig
    from lerobot_webrtc.arms import AsyncArm, SimulatedArm
    from lerobot_webrtc.native_protocol import NATIVE_LABEL
    from lerobot_webrtc.peer import Peer
    from lerobot_webrtc.video_sources import SyntheticVideoTrack

    url, _ = signaling_server
    config = WebRTCRobotConfig(
        signaling=url,
        room="sdk-workflow",
        remote_id="simulated",
        allow_physical=False,
        id="native-proxy",
        calibration_dir=tmp_path / "proxy-calibration",
        connect_timeout=5,
        request_timeout=1,
        observation_timeout=0.5,
        camera_name="front" if camera else None,
        video_width=320,
        video_height=240,
        video_fps=10,
    )
    leader_config = SO101LeaderConfig(
        port="notopened",
        id="synthetic-leader",
        use_degrees=False,
        calibration_dir=tmp_path / "leader-calibration",
    )
    trace = _observe_real_robot(monkeypatch)
    leader = _leader(leader_config, trace)

    def make_leader(received):
        assert received is leader_config
        return leader

    monkeypatch.setattr(script, "make_teleoperator_from_config", make_leader)
    device = SimulatedArm(delta=0.5, model="so101")
    arm = AsyncArm(device, "follower")
    source = SyntheticVideoTrack(config.video_settings()) if camera else None
    peer = Peer(
        "follower",
        url,
        config.room,
        TOKEN,
        channel_label=NATIVE_LABEL,
        video_source=source,
    )
    return SimpleNamespace(
        config=config,
        leader_config=leader_config,
        trace=trace,
        leader=leader,
        device=device,
        arm=arm,
        peer=peer,
        source=source,
    )


async def _run_workflow(workflow, operation):
    from lerobot_webrtc.native_host import run_robot_host

    host = asyncio.create_task(
        run_robot_host(
            workflow.arm,
            workflow.peer,
            "so101",
            30,
            0.3,
            None,
        )
    )
    # Keep the test signaling server/host alive while the synchronous SDK runs.
    worker = asyncio.create_task(asyncio.to_thread(operation))
    try:
        result = await asyncio.wait_for(asyncio.shield(worker), 45)
        (outcome,) = await asyncio.wait_for(
            asyncio.gather(host, return_exceptions=True),
            3,
        )
        return result, outcome
    finally:
        host.cancel()
        await asyncio.wait_for(asyncio.gather(host, return_exceptions=True), 5)
        # SDK record only disconnects an is_connected Robot. A terminal watchdog
        # can clear that flag during dataset finalization; still join owned I/O.
        if workflow.trace.robot is not None:
            await asyncio.wait_for(asyncio.to_thread(workflow.trace.robot.disconnect), 5)
        await asyncio.wait_for(asyncio.shield(worker), 5)
        if workflow.leader.is_connected:
            workflow.leader.disconnect()


def _assert_workflow(workflow, cfg, capfd):
    trace = workflow.trace
    assert trace.robot.config is workflow.config
    assert trace.events[:2] == ["teleop.connect", "robot.connect"]
    assert workflow.device.applied == len(trace.results) >= 3
    assert len(trace.observations) == len(trace.requests) == len(trace.results)
    assert all(action == workflow.leader.request for action in trace.requests)
    assert trace.observations[0]["shoulder_pan.pos"] == 0
    assert any(obs["shoulder_pan.pos"] > 0 for obs in trace.observations)
    assert all(obs["shoulder_pan.pos"] < 20 for obs in trace.observations)
    # The result is the real simulated driver's clamped target, not a request echo.
    assert [result["shoulder_pan.pos"] for result in trace.results] == [
        0.5 * (index + 1) for index in range(len(trace.results))
    ]
    assert workflow.device.held and not workflow.device.connected
    assert not workflow.leader.is_connected
    assert workflow.peer.closed.is_set()
    assert workflow.peer.pc.connectionState == "closed"
    assert not trace.robot.is_connected
    assert not trace.client._thread.is_alive()
    assert TOKEN not in repr(asdict(cfg))
    # SDK init_logging replaces root handlers, including pytest's caplog
    # handler. Capture actual stdout/stderr instead of missing the SDK logs.
    captured = capfd.readouterr()
    output = captured.out + captured.err
    assert "'token_env': 'LEROBOT_WEBRTC_TOKEN'" in output
    assert TOKEN not in output
    if workflow.source is not None:
        assert workflow.source.readyState == "ended"


def test_native_bootstrap_console_help_exports_config(native_sdk_env):
    # Invoke the installed console entry point, not a hand-rolled parser/import.
    completed = subprocess.run(
        [
            str(Path(sys.executable).with_name("lerobot-teleoperate")),
            "--robot.discover_packages_path=lerobot_robot_webrtc",
            "--robot.type=webrtc",
            "--help",
        ],
        cwd=Path(__file__).resolve().parents[1],
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=60,
    )
    output = completed.stdout + completed.stderr
    assert completed.returncode == 0, output
    for field in (
        "model",
        "signaling",
        "room",
        "token_env",
        "allow_physical",
        "remote_id",
        "connect_timeout",
        "camera_name",
        "video_width",
        "video_height",
        "video_fps",
        "video_codec",
        "video_timeout",
        "request_timeout",
        "observation_timeout",
        "stun",
        "turn",
        "turn_username_env",
        "turn_password_env",
        "id",
        "calibration_dir",
    ):
        assert f"--robot.{field}" in output
    assert TOKEN not in output


@pytest.mark.parametrize("camera", [False, True], ids=["joints", "synthetic-rgb"])
async def test_sdk_teleoperate_native_host(
    native_sdk_env,
    monkeypatch,
    tmp_path,
    signaling_server,
    capfd,
    camera,
):
    import numpy as np
    from lerobot.scripts import lerobot_teleoperate as script

    workflow = _workflow(monkeypatch, tmp_path, signaling_server, script, camera=camera)
    cfg = script.TeleoperateConfig(
        teleop=workflow.leader_config,
        robot=workflow.config,
        fps=30,
        teleop_time_s=0.5,
        display_data=False,
    )
    _, outcome = await _run_workflow(workflow, lambda: script.teleoperate.__wrapped__(cfg))
    assert outcome is None or isinstance(outcome, ConnectionError), repr(outcome)
    _assert_workflow(workflow, cfg, capfd)
    if camera:
        images = [obs["front"] for obs in workflow.trace.observations]
        assert all(image.shape == (240, 320, 3) and image.dtype == np.uint8 for image in images)
        assert not np.shares_memory(images[0], images[1])
        saved = images[1].copy()
        images[0][:] = 0
        np.testing.assert_array_equal(images[1], saved)
        assert np.ptp(saved) > 0


@pytest.mark.parametrize(
    ("camera", "video"),
    [(False, False), (True, False), (True, True)],
    ids=["joints", "synthetic-rgb-png", "synthetic-rgb-mp4-h264"],
)
async def test_sdk_record_one_local_native_episode(
    native_sdk_env,
    monkeypatch,
    tmp_path,
    signaling_server,
    capfd,
    camera,
    video,
):
    import numpy as np
    import pyarrow.parquet as parquet
    from lerobot.configs.dataset import DatasetRecordConfig
    from lerobot.datasets import LeRobotDataset
    from lerobot.scripts import lerobot_record as script

    encoder_options = {}
    if video:
        from lerobot.configs.video import RGBEncoderConfig

        # Legitimate SDK file-encoding options, not WebRTC codec settings.
        # Explicit software H.264 avoids hardware/auto-selection and default AV1.
        encoder_options = {
            "rgb_encoder": RGBEncoderConfig(
                vcodec="h264",
                preset="ultrafast",
                crf=18,
                video_backend="pyav",
            ),
            "encoder_threads": 1,
        }

    events = {"exit_early": False, "stop_recording": False, "rerecord_episode": False}
    keyboard_calls = []

    def inert_keyboard():
        keyboard_calls.append(True)
        return None, events

    monkeypatch.setattr(script, "init_keyboard_listener", inert_keyboard)
    monkeypatch.setattr(script, "log_say", lambda *args, **kwargs: None)
    workflow = _workflow(monkeypatch, tmp_path, signaling_server, script, camera=camera)
    cfg = script.RecordConfig(
        robot=workflow.config,
        teleop=workflow.leader_config,
        dataset=DatasetRecordConfig(
            repo_id="test/native-demo",
            root=tmp_path / "dataset",
            single_task="Synthetic reach",
            fps=30,
            episode_time_s=0.6,
            reset_time_s=0,
            num_episodes=1,
            push_to_hub=False,
            video=video,
            no_stamp=True,
            num_image_writer_processes=0,
            num_image_writer_threads_per_camera=1,
            **encoder_options,
        ),
        display_data=False,
        play_sounds=False,
    )
    dataset, outcome = await _run_workflow(workflow, lambda: script.record.__wrapped__(cfg))
    # Saving/finalization may outlast the unchanged 300ms motion watchdog. That
    # is a terminal STOP, not evidence of support for pauses/resume/episode two.
    assert outcome is None or isinstance(outcome, (ConnectionError, TimeoutError)), repr(outcome)
    if isinstance(outcome, TimeoutError):
        assert "action watchdog expired" in str(outcome)
    _assert_workflow(workflow, cfg, capfd)
    assert keyboard_calls == [True]
    assert type(dataset) is LeRobotDataset
    assert dataset.repo_id == "test/native-demo"
    assert dataset.fps == 30 and dataset.num_episodes == 1
    assert dataset.num_frames == workflow.device.applied
    assert dataset.meta.robot_type == "webrtc"
    assert dataset._is_finalized
    joints = list(workflow.leader.action_features)
    assert dataset.features["action"]["names"] == joints
    assert dataset.features["observation.state"]["names"] == joints
    files = sorted(dataset.root.glob("data/**/*.parquet"))
    assert files
    rows = [row for file in files for row in parquet.read_table(file).to_pylist()]
    assert len(rows) == dataset.num_frames
    for index, row in enumerate(rows):
        assert row["episode_index"] == 0
        assert row["frame_index"] == index
        assert row["timestamp"] == pytest.approx(index / 30)
        np.testing.assert_allclose(
            row["observation.state"],
            [workflow.trace.observations[index][key] for key in joints],
        )
        # Known SDK caveat: _sent_action is ignored. Labels are processed
        # REQUESTED actions, not the native host's actual clamped dispatches.
        np.testing.assert_allclose(row["action"], [workflow.leader.request[key] for key in joints])
        assert (
            row["action"][joints.index("shoulder_pan.pos")]
            > workflow.trace.results[index]["shoulder_pan.pos"]
        )
    if video:
        _assert_mp4_episode(dataset, workflow)
    elif camera:
        from io import BytesIO

        from PIL import Image

        feature = dataset.features["observation.images.front"]
        assert feature["dtype"] == "image" and tuple(feature["shape"]) == (240, 320, 3)
        for index, row in enumerate(rows):
            image = row["observation.images.front"]
            with Image.open(BytesIO(image["bytes"])) as decoded:
                assert decoded.format == "PNG" and decoded.mode == "RGB"
                np.testing.assert_array_equal(
                    np.asarray(decoded),
                    workflow.trace.observations[index]["front"],
                )
        assert not list(dataset.root.rglob("*.mp4"))


def _assert_mp4_episode(dataset, workflow):
    import json

    import av
    import numpy as np

    key = "observation.images.front"
    feature = dataset.features[key]
    assert feature["dtype"] == "video"
    assert tuple(feature["shape"]) == (240, 320, 3)
    assert dataset.meta.video_keys == [key]
    info = feature["info"]
    assert info["video.codec"] == "h264"
    assert info["video.pix_fmt"] == "yuv420p"
    assert (info["video.height"], info["video.width"], info["video.fps"]) == (240, 320, 30)
    persisted = json.loads((dataset.root / "meta" / "info.json").read_text())
    assert persisted["features"][key] == json.loads(json.dumps(feature))
    videos = list(dataset.root.glob("videos/**/*.mp4"))
    assert len(videos) == 1 and videos[0].stat().st_size > 0

    # Decode the SDK's actual finalized file, without DatasetReader/TorchCodec.
    with av.open(str(videos[0])) as container:
        assert len(container.streams.video) == 1
        assert not container.streams.audio
        stream = container.streams.video[0]
        assert stream.codec_context.name == "h264"
        assert stream.average_rate == 30
        frames = list(container.decode(video=0))
    assert len(frames) == dataset.num_frames == workflow.device.applied
    for index, frame in enumerate(frames):
        pixels = frame.to_ndarray(format="rgb24")
        assert pixels.shape == (240, 320, 3) and pixels.dtype == np.uint8
        assert frame.time == pytest.approx(index / 30)
        assert np.ptp(pixels) > 0
        # H.264 is lossy, unlike the PNG case; check content without claiming
        # byte-exact RGB preservation through chroma subsampling/compression.
        source = workflow.trace.observations[index]["front"]
        assert np.mean(np.abs(pixels.astype(float) - source.astype(float))) < 10
