import json
import sys
from types import ModuleType, SimpleNamespace

import pytest

from examples.livekit import probe


@pytest.fixture
def fake_livekit(monkeypatch):
    state = SimpleNamespace(
        published=[], closed=False, packets=[], acknowledge=True, receipt_override={}, ready=True
    )

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
            if state.ready:
                self.callbacks["data_received"](
                    SimpleNamespace(
                        participant=SimpleNamespace(identity="expected-peer"),
                        topic=probe.TOPIC,
                        data=b'{"kind":"probe-ready"}',
                    )
                )

        async def publish(self, payload, **options):
            data = json.loads(payload)
            state.published.append((data, options))
            if data.get("kind") == "network-probe" and data["seq"] == 4 and state.acknowledge:
                self.callbacks["data_received"](
                    SimpleNamespace(
                        participant=SimpleNamespace(identity="expected-peer"),
                        topic=probe.TOPIC,
                        data=json.dumps(
                            {
                                "kind": "probe-receipt",
                                "probe_id": data["probe_id"],
                                "received": 5,
                                **state.receipt_override,
                            }
                        ).encode(),
                    )
                )

        async def disconnect(self):
            state.closed = True

    module = ModuleType("livekit")
    module.rtc = SimpleNamespace(Room=Room, RoomOptions=lambda **kwargs: SimpleNamespace(**kwargs))
    monkeypatch.setitem(sys.modules, "livekit", module)
    return state


async def test_livekit_sender_is_targeted_unreliable_probe_not_robot_control(fake_livekit):
    result = await probe.run_probe("send", "expected-peer", 1, "wss://example.test", "fake-jwt")
    assert result["messages_submitted"] == 5
    assert result["delivery_verified_by_sender"] is True
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
    def packet(
        kind="network-probe", seq=0, peer="expected-peer", topic=probe.TOPIC, run_id="a" * 32
    ):
        return SimpleNamespace(
            participant=SimpleNamespace(identity=peer),
            topic=topic,
            data=json.dumps({"kind": kind, "seq": seq, "probe_id": run_id}).encode(),
        )

    fake_livekit.packets = [
        packet(),
        packet(),
        packet(seq=1),
        packet(peer="other-peer"),
        packet(topic="robot-actions"),
        packet(kind="action"),
        packet(seq=True),
        packet(seq=2, run_id="b" * 32),
        packet(seq=3, run_id=None),
    ]
    result = await probe.run_probe("receive", "expected-peer", 0, "wss://example.test", "fake-jwt")
    assert result["unique_messages_received"] == 2
    assert result["motor_access"] is False
    assert fake_livekit.closed


async def test_livekit_sender_keeps_room_open_for_matching_receipt(fake_livekit):
    import asyncio

    fake_livekit.acknowledge = False
    task = asyncio.create_task(
        probe.run_probe("send", "expected-peer", 0.1, "wss://example.test", "fake-jwt")
    )
    while len(fake_livekit.published) < 5:
        await asyncio.sleep(0.01)
    assert not fake_livekit.closed
    result = await task
    assert result["messages_submitted"] == 5
    assert result["delivery_verified_by_sender"] is False
    assert fake_livekit.closed


async def test_livekit_sender_does_not_publish_before_receiver_is_ready(fake_livekit):
    fake_livekit.ready = False
    with pytest.raises(TimeoutError):
        await probe.run_probe("send", "expected-peer", 0.01, "wss://example.test", "fake-jwt")
    assert not fake_livekit.published
    assert fake_livekit.closed


async def test_livekit_receiver_sends_receipt_only_for_five_unique_packets(fake_livekit):
    fake_livekit.packets = [
        SimpleNamespace(
            participant=SimpleNamespace(identity="expected-peer"),
            topic=probe.TOPIC,
            data=json.dumps({"kind": "network-probe", "seq": seq, "probe_id": "a" * 32}).encode(),
        )
        for seq in range(5)
    ]
    result = await probe.run_probe(
        "receive", "expected-peer", 0.01, "wss://example.test", "fake-jwt"
    )
    assert result["unique_messages_received"] == 5
    assert fake_livekit.published == [
        (
            {"kind": "probe-receipt", "probe_id": "a" * 32, "received": 5},
            {"reliable": True, "destination_identities": ["expected-peer"], "topic": probe.TOPIC},
        )
    ]
    assert fake_livekit.closed


@pytest.mark.parametrize(
    "override", [{"probe_id": "b" * 32}, {"received": True}, {"received": 4}, {"kind": "action"}]
)
async def test_livekit_sender_rejects_wrong_or_incomplete_receipts(fake_livekit, override):
    fake_livekit.receipt_override = override
    result = await probe.run_probe("send", "expected-peer", 0.01, "wss://example.test", "fake-jwt")
    assert result["delivery_verified_by_sender"] is False
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
