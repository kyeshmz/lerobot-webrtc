"""Native Robot RPC framing, separate from the standalone teleoperation channel."""

from __future__ import annotations

import json
import re

from .models import arm_model

NATIVE_LABEL = "lerobot-robot-v1"
MAX_NATIVE_MESSAGE = 16384
KINDS = {"hello", "ready", "state", "action", "result", "stop"}


def native_message(model: str, session: str, kind: str, **fields) -> str:
    if (
        not isinstance(kind, str)
        or kind not in KINDS
        or not isinstance(session, str)
        or not re.fullmatch(r"[a-f0-9]{32}", session)
        or fields.keys() & {"v", "type", "model", "units", "session"}
    ):
        raise ValueError("Invalid native Robot envelope")
    return json.dumps(
        {
            "v": 1,
            "type": kind,
            "model": model,
            "units": arm_model(model).units,
            "session": session,
            **fields,
        },
        separators=(",", ":"),
        allow_nan=False,
    )


def decode_native(message: str | bytes, model: str, session: str | None = None) -> dict:
    if not isinstance(message, str) or not 0 < len(message) <= MAX_NATIVE_MESSAGE:
        raise ValueError("Expected a bounded native Robot JSON message")
    data = json.loads(message)
    if not isinstance(data, dict) or (
        type(data.get("v")) is not int
        or data["v"] != 1
        or not isinstance(data.get("type"), str)
        or data["type"] not in KINDS
        or data.get("model") != model
        or data.get("units") != arm_model(model).units
    ):
        raise ValueError("Incompatible native Robot protocol, model or units")
    epoch = data.get("session")
    if not isinstance(epoch, str) or not re.fullmatch(r"[a-f0-9]{32}", epoch):
        raise ValueError("Invalid native Robot session")
    if session is not None and epoch != session:
        raise ValueError("Native Robot session changed; reconnect explicitly")
    return data


def sequence(value: object) -> int:
    if type(value) is not int or not 0 <= value < 2**53:
        raise ValueError("Invalid native Robot sequence")
    return value


def permit(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{32}", value):
        raise ValueError("Invalid native Robot freshness permit")
    return value
