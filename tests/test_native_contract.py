import json
from dataclasses import asdict

import pytest

from lerobot_webrtc.native_options import RemoteRobotOptions
from lerobot_webrtc.native_protocol import (
    MAX_NATIVE_MESSAGE,
    decode_native,
    native_message,
    permit,
    sequence,
)


@pytest.mark.parametrize(
    "fields",
    [
        {"v": True},
        {"v": 2},
        {"type": []},
        {"type": "unrecognized"},
        {"model": "so100"},
        {"units": "degrees"},
        {"session": "bad"},
        {"session": "B" * 32},
    ],
)
def test_native_wire_rejects_incompatible_headers(fields):
    data = json.loads(native_message("so101", "a" * 32, "hello"))
    data.update(fields)
    with pytest.raises(ValueError):
        decode_native(json.dumps(data), "so101", "a" * 32)


@pytest.mark.parametrize("message", [b"{}", "", "[]", "x" * (MAX_NATIVE_MESSAGE + 1)])
def test_native_wire_rejects_unbounded_non_text_or_non_object_payloads(message):
    with pytest.raises(ValueError):
        decode_native(message, "so101")


def test_native_encoder_cannot_overwrite_protocol_headers():
    with pytest.raises(ValueError):
        native_message("so101", "a" * 32, "hello", v=2)


@pytest.mark.parametrize("value", [True, -1, 2**53, 1.0, None])
def test_native_sequence_is_a_bounded_exact_integer(value):
    with pytest.raises(ValueError):
        sequence(value)


@pytest.mark.parametrize("value", [None, 1, "A" * 32, "b" * 31])
def test_native_freshness_permit_is_exact_lowercase_hex(value):
    with pytest.raises(ValueError):
        permit(value)


@pytest.mark.parametrize(
    "values",
    [
        {"model": "unknown"},
        {"room": "has/slash"},
        {"token_env": "SECRET=value"},
        {"allow_physical": 1},
        {"connect_timeout": float("nan")},
        {"request_timeout": 100},
        {"observation_timeout": 0},
        {"camera_name": "front.png"},
        {"video_width": 641},
        {"signaling": "ws://user:password@host"},
        {"signaling": "wss://host?token=secret"},
        {"stun": ["https://host"]},
        {"turn": "turn:user@host"},
    ],
)
def test_native_configuration_rejects_invalid_modes_and_secret_uris(values):
    with pytest.raises(ValueError):
        RemoteRobotOptions(**values)


def test_native_configuration_contains_environment_names_not_resolved_secrets(monkeypatch):
    monkeypatch.setenv("SDK_TEST_TOKEN", "private-value-not-config")
    options = RemoteRobotOptions(token_env="SDK_TEST_TOKEN")
    assert asdict(options)["token_env"] == "SDK_TEST_TOKEN"
    assert "private-value-not-config" not in repr(asdict(options))
