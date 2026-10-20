"""Bounded, versioned commands with follower-issued freshness permits.

The remote clock is never trusted. A permit's lifetime is measured exclusively
with the follower's monotonic clock and includes the network round trip.
"""

from __future__ import annotations

import json
import math
import secrets
from dataclasses import dataclass

from .models import arm_model

VERSION = 1
UNITS = arm_model("so101").units
JOINTS = arm_model("so101").joints
MAX_MESSAGE = 4096


def decode(message: str | bytes, model: str, kind: str | tuple[str, ...]) -> dict:
    if not isinstance(message, str) or len(message) > MAX_MESSAGE:
        raise ValueError("Expected a small JSON text message")
    data = json.loads(message)
    if not isinstance(data, dict):
        raise ValueError("Expected an object")
    if (
        type(data.get("v")) is not int
        or data["v"] != VERSION
        or data.get("type") not in ((kind,) if isinstance(kind, str) else kind)
        or data.get("model") != model
        or data.get("units") != arm_model(model).units
    ):
        raise ValueError("Incompatible protocol, arm model, or position units")
    token = data.get("permit")
    if not isinstance(token, str) or len(token) != 32:
        raise ValueError("Invalid freshness permit")
    return data


def envelope(model: str, kind: str, **fields) -> str:
    return json.dumps(
        {"v": VERSION, "type": kind, "model": model, "units": arm_model(model).units, **fields},
        separators=(",", ":"),
        allow_nan=False,
    )


def _validate_positions(action: object, model: str, bounds) -> dict[str, float]:
    schema = arm_model(model)
    if not isinstance(action, dict) or set(action) != set(schema.joints):
        raise ValueError(f"Action must contain exactly the {model} joint positions")
    result = {}
    for joint, minimum, maximum in bounds:
        value = action[joint]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"Non-numeric position for {joint}")
        if not math.isfinite(value) or not minimum <= value <= maximum:
            raise ValueError(f"Out-of-range {schema.units} position for {joint}")
        result[joint] = float(value)
    return result


def validate_action(action: object, model: str = "so101") -> dict[str, float]:
    return _validate_positions(action, model, arm_model(model).bounds)


def validate_observation(position: object, model: str = "so101") -> dict[str, float]:
    return _validate_positions(position, model, arm_model(model).observation_bounds)


def hold_action(position: object, model: str) -> dict[str, float]:
    observed = validate_observation(position, model)
    return {
        joint: max(low, min(high, observed[joint])) for joint, low, high in arm_model(model).bounds
    }


@dataclass(frozen=True)
class Command:
    action: dict[str, float]
    deadline: float


class FollowerInbox:
    """Latest-only mailbox. No motion-command queue and no remote timestamps."""

    def __init__(self, model: str, timeout: float, simulated: bool = True):
        arm_model(model)
        self.model = model
        self.timeout = timeout
        self.simulated = simulated
        self.permits: dict[str, float] = {}
        self.sequence = -1
        self.pending: Command | None = None
        self.observation_sequence = 0

    def issue_permit(self, now: float, position: dict[str, float] | None = None) -> str:
        self.permits = {key: end for key, end in self.permits.items() if end > now}
        token = secrets.token_hex(16)
        self.permits[token] = now + self.timeout
        fields = {}
        if self.model == "b601-rs":
            fields = {
                "position": validate_observation(position, self.model),
                "simulated": self.simulated,
                "observation_seq": self.observation_sequence,
            }
            self.observation_sequence += 1
        return envelope(self.model, "permit", permit=token, **fields)

    def receive(self, message: str | bytes, now: float) -> bool:
        data = decode(message, self.model, "action")
        sequence = data.get("seq")
        if type(sequence) is not int or not 0 <= sequence < 2**53:
            raise ValueError("Invalid sequence number")
        action = validate_action(data.get("action"), self.model)
        if sequence <= self.sequence:
            return False
        deadline = self.permits.get(data["permit"], 0)
        if now >= deadline:
            return False
        self.sequence = sequence
        self.pending = Command(action, deadline)
        return True

    def take(self, now: float) -> Command | None:
        command, self.pending = self.pending, None
        return command if command is not None and now < command.deadline else None


class LeaderInbox:
    def __init__(self, model: str):
        arm_model(model)
        self.model = model
        self.permit: str | None = None
        self.sequence = 0
        self.position: dict[str, float] | None = None
        self.simulated: bool | None = None
        self.observation_sequence = -1

    def receive(self, message: str | bytes) -> str | None:
        kinds = ("hello", "permit") if self.model == "b601-rs" else "permit"
        data = decode(message, self.model, kinds)
        if self.model == "b601-rs":
            if type(data.get("simulated")) is not bool:
                raise ValueError("B601 permits require an explicit simulation marker")
            if self.simulated is not None and self.simulated != data["simulated"]:
                raise ValueError("Follower simulation marker changed during the session")
            if data["type"] == "hello":
                self.simulated = data["simulated"]
                return envelope(self.model, "ready", permit=data["permit"], role="leader")
            position = validate_observation(data.get("position"), self.model)
            sequence = data.get("observation_seq")
            if type(sequence) is not int or not 0 <= sequence < 2**53:
                raise ValueError("Invalid B601 observation sequence")
            if sequence <= self.observation_sequence:
                return
            self.position = position
            self.observation_sequence = sequence
            self.simulated = data["simulated"]
        self.permit = data["permit"]

    def command(self, action: dict[str, float], permit: str | None = None) -> str | None:
        permit = permit or self.permit
        if permit is None:
            return None
        data = envelope(
            self.model,
            "action",
            permit=permit,
            seq=self.sequence,
            action=validate_action(action, self.model),
        )
        self.sequence += 1
        return data
