import json
import sys
from types import ModuleType, SimpleNamespace

import pytest

from examples.livekit import probe


@pytest.fixture
def fake_livekit(monkeypatch):
    state = SimpleNamespace(published=[], closed=False, packets=[])

    class Room:
        def __init__(self):
            self.callbacks = {}
            self.remote_participants = {"peer": SimpleNamespace(identity="expected-peer")}
            self.local_participant = SimpleNamespace(publish_data=self.publish)

        def on(self, event):
            def register(callback):
                self.callbacks[event] = callback
                return callback

            return register

        async def connect(self, url, token, options):
            assert options.auto_subscribe is False
            for packet in state.packets:
                self.callbacks["data_received"](packet)

        async def publish(self, payload, **options):
            state.published.append((json.loads(payload), options))

        async def disconnect(self):
            state.closed = True

    module = ModuleType("livekit")
    module.rtc = SimpleNamespace(Room=Room, RoomOptions=lambda **kwargs: SimpleNamespace(**kwargs))
    monkeypatch.setitem(sys.modules, "livekit", module)
    return state


async def test_livekit_sender_is_targeted_unreliable_probe_not_robot_control(fake_livekit):
    result = await probe.run_probe("send", "expected-peer", 1, "wss://example.test", "fake-jwt")
    assert result["messages_submitted"] == 5
    assert result["delivery_verified_by_sender"] is False
    assert result["motor_access"] is False
    assert fake_livekit.closed
    for payload, options in fake_livekit.published:
        assert payload["kind"] == "network-probe"
        assert options == {
            "reliable": False,
            "destination_identities": ["expected-peer"],
            "topic": probe.TOPIC,
        }


async def test_livekit_receiver_filters_identity_topic_and_duplicates(fake_livekit):
    def packet(kind="network-probe", seq=0, peer="expected-peer", topic=probe.TOPIC):
        return SimpleNamespace(
            participant=SimpleNamespace(identity=peer),
            topic=topic,
            data=json.dumps({"kind": kind, "seq": seq}).encode(),
        )

    fake_livekit.packets = [
        packet(),
        packet(),
        packet(seq=1),
        packet(peer="other-peer"),
        packet(topic="robot-actions"),
        packet(kind="action"),
        packet(seq=True),
    ]
    result = await probe.run_probe("receive", "expected-peer", 0, "wss://example.test", "fake-jwt")
    assert result["unique_messages_received"] == 2
    assert result["motor_access"] is False
    assert fake_livekit.closed


@pytest.mark.parametrize(
    "url",
    [
        "https://example.test",
        "ws://example.test",
        "wss://user:pass@example.test",
        "wss://example.test?token=secret",
        "wss://example.test:99999",
    ],
)
def test_livekit_url_policy_rejects_insecure_or_embedded_credentials(url):
    with pytest.raises(ValueError):
        probe.connection_settings({"LIVEKIT_URL": url, "LIVEKIT_TOKEN": "fake-jwt"})


def test_livekit_local_development_url_is_explicitly_allowed():
    assert probe.connection_settings(
        {"LIVEKIT_URL": "ws://localhost:7880", "LIVEKIT_TOKEN": "fake-jwt"}
    ) == ("ws://localhost:7880", "fake-jwt")
