"""Synchronous SDK adapter for the separately owned native WebRTC client."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import fields
from typing import TYPE_CHECKING

from lerobot.robots.robot import Robot
from lerobot.utils.errors import DeviceAlreadyConnectedError, DeviceNotConnectedError

from lerobot_webrtc.models import arm_model
from lerobot_webrtc.native_options import RemoteRobotOptions

from .config_webrtc import WebRTCRobotConfig

if TYPE_CHECKING:
    from numpy.typing import NDArray

    from lerobot_webrtc.native_client import RemoteRobotClient


class _RemoteCamera:
    """A frame view of the robot's client, not a separately owned camera device."""

    def __init__(self, robot: WebRTCRobot):
        self._robot = robot
        self.fps = robot.config.video_fps
        self.width = robot.config.video_width
        self.height = robot.config.video_height

    @property
    def is_connected(self) -> bool:
        return self._robot.is_connected

    def read(self) -> NDArray:
        frame = self._robot._require_client().get_frame()
        # Conversion and the independent RGB copy run on the SDK caller's thread.
        image = frame.to_ndarray(format="rgb24").copy()
        if image.shape != (self.height, self.width, 3):
            raise ValueError("Remote RGB frame does not match the configured camera shape")
        return image

    def async_read(self, timeout_ms: float = 200) -> NDArray:
        """Read the latest age-checked frame; video_timeout governs its freshness."""
        return self.read()


class WebRTCRobot(Robot):
    config_class = WebRTCRobotConfig
    name = "webrtc"

    def __init__(self, config: WebRTCRobotConfig):
        self._client: RemoteRobotClient | None = None
        super().__init__(config)
        self.config = config
        self._configured_options = {
            field.name: deepcopy(getattr(config, field.name))
            for field in fields(RemoteRobotOptions)
        }
        self._action_features = dict.fromkeys(arm_model(config.model).joints, float)
        self._observation_features = self._action_features.copy()
        self.cameras: dict[str, _RemoteCamera] = {}
        if config.camera_name is not None:
            camera = _RemoteCamera(self)
            self.cameras[config.camera_name] = camera
            self._observation_features[config.camera_name] = (camera.height, camera.width, 3)

    @property
    def observation_features(self) -> dict[str, type | tuple[int, int, int]]:
        return self._observation_features.copy()

    @property
    def action_features(self) -> dict[str, type]:
        return self._action_features.copy()

    @property
    def is_connected(self) -> bool:
        return self._client is not None and self._client.is_connected

    @property
    def is_calibrated(self) -> bool:
        # Only this transport proxy has no calibration. Host readiness is checked
        # by the client's handshake, not inferred from this SDK property.
        return True

    def connect(self, calibrate: bool = True) -> None:
        if self.is_connected:
            raise DeviceAlreadyConnectedError()
        # A stopped client can still own a loop/thread. Join it before replacing it.
        self.disconnect()
        if any(
            getattr(self.config, name) != value for name, value in self._configured_options.items()
        ):
            raise ValueError(
                "Create a new WebRTCRobot after changing its connection or feature config"
            )
        from lerobot_webrtc.native_client import RemoteRobotClient

        self._client = RemoteRobotClient(self.config)
        try:
            self._client.connect()
            if not self._client.is_connected:
                raise ConnectionError("Native Robot client did not become ready")
            # calibrate=False never bypasses the host's physical readiness checks.
            self.configure()
        except BaseException as error:
            try:
                self.disconnect()
            except BaseException as cleanup_error:
                error.add_note(
                    "Client cleanup failed; disconnect before attempting another connect"
                )
                raise error from cleanup_error
            raise

    def calibrate(self) -> None:
        raise RuntimeError("Remote calibration is unavailable; calibrate hardware locally on host")

    def configure(self) -> None:
        """No remote hardware mutation; the host exclusively configures its motors."""

    def _require_client(self) -> RemoteRobotClient:
        if not self.is_connected:
            raise DeviceNotConnectedError(
                "Native Robot is disconnected. Restart/rearm the host as needed, "
                "then call connect()."
            )
        return self._client

    def get_observation(self) -> dict[str, object]:
        # Pin the measured sample's action permit BEFORE frame conversion or any
        # computation by the caller. Frames must never refresh that permit.
        observation = self._require_client().get_observation().copy()
        for name, camera in self.cameras.items():
            observation[name] = camera.read()
        return observation

    def send_action(self, action: dict[str, float]) -> dict[str, float]:
        # The correlated result is the actual dispatched target, not the request.
        return self._require_client().send_action(action).copy()

    def disconnect(self) -> None:
        client = self._client
        if client is not None:
            # Do not gate cleanup on is_connected or discard ownership on failure.
            client.disconnect()
            self._client = None
