"""Bounded, versioned commands with follower-issued freshness permits.

The remote clock is never trusted. A permit's lifetime is measured exclusively
with the follower's monotonic clock and includes the network round trip.
"""

from __future__ import annotations

import json
import math
import secrets
from dataclasses import dataclass

VERSION = 1
UNITS = "lerobot-normalized"
JOINTS = (
    "shoulder_pan.pos",
    "shoulder_lift.pos",
    "elbow_flex.pos",
    "wrist_flex.pos",
    "wrist_roll.pos",
    "gripper.pos",
)
MAX_MESSAGE = 4096


def decode(message: str | bytes, model: str, kind: str) -> dict:
    if not isinstance(message, str) or len(message) > MAX_MESSAGE:
        raise ValueError("Expected a small JSON text message")
    data = json.loads(message)
    if not isinstance(data, dict):
        raise ValueError("Expected an object")
    if (
        type(data.get("v")) is not int
        or data["v"] != VERSION
        or data.get("type") != kind
        or data.get("model") != model
        or data.get("units") != UNITS
    ):
        raise ValueError("Incompatible protocol, arm model, or position units")
    token = data.get("permit")
    if not isinstance(token, str) or len(token) != 32:
        raise ValueError("Invalid freshness permit")
    return data


def envelope(model: str, kind: str, **fields) -> str:
    return json.dumps(
        {"v": VERSION, "type": kind, "model": model, "units": UNITS, **fields},
        separators=(",", ":"),
        allow_nan=False,
    )


def validate_action(action: object) -> dict[str, float]:
    if not isinstance(action, dict) or set(action) != set(JOINTS):
        raise ValueError("Action must contain exactly the six SO arm joint positions")
    result = {}
    for joint, value in action.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"Non-numeric position for {joint}")
        minimum = 0 if joint == "gripper.pos" else -100
        if not math.isfinite(value) or not minimum <= value <= 100:
            raise ValueError(f"Out-of-range normalized position for {joint}")
        result[joint] = float(value)
    return result


@dataclass(frozen=True)
class Command:
    action: dict[str, float]
    deadline: float


class FollowerInbox:
    """Latest-only mailbox. No motion-command queue and no remote timestamps."""

    def __init__(self, model: str, timeout: float):
        self.model = model
        self.timeout = timeout
        self.permits: dict[str, float] = {}
        self.sequence = -1
        self.pending: Command | None = None

    def issue_permit(self, now: float) -> str:
        self.permits = {key: end for key, end in self.permits.items() if end > now}
        token = secrets.token_hex(16)
        self.permits[token] = now + self.timeout
        return envelope(self.model, "permit", permit=token)

    def receive(self, message: str | bytes, now: float) -> bool:
        data = decode(message, self.model, "action")
        sequence = data.get("seq")
        if type(sequence) is not int or not 0 <= sequence < 2**53:
            raise ValueError("Invalid sequence number")
        action = validate_action(data.get("action"))
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
        self.model = model
        self.permit: str | None = None
        self.sequence = 0

    def receive(self, message: str | bytes) -> None:
        self.permit = decode(message, self.model, "permit")["permit"]

    def command(self, action: dict[str, float], permit: str | None = None) -> str | None:
        permit = permit or self.permit
        if permit is None:
            return None
        data = envelope(
            self.model,
            "action",
            permit=permit,
            seq=self.sequence,
            action=validate_action(action),
        )
        self.sequence += 1
        return data
