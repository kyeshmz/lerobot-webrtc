import asyncio

import pytest
from aiohttp import ClientSession, WSServerHandshakeError

from lerobot_webrtc.signaling import ROOMS, shutdown

from .conftest import TOKEN


async def test_authentication_and_room_validation(signaling_server):
    url, _ = signaling_server
    async with ClientSession() as client:
        with pytest.raises(WSServerHandshakeError) as rejected:
            await client.ws_connect(f"{url}/ws/demo/leader")
        assert rejected.value.status == 401
        with pytest.raises(WSServerHandshakeError) as rejected:
            await client.ws_connect(
                f"{url}/ws/demo/invalid", headers={"Authorization": f"Bearer {TOKEN}"}
            )
        assert rejected.value.status == 400


async def test_room_capacity_ready_and_sdp_relay(signaling_server):
    url, app = signaling_server
    async with ClientSession(headers={"Authorization": f"Bearer {TOKEN}"}) as client:
        leader = await client.ws_connect(f"{url}/ws/demo/leader")
        with pytest.raises(WSServerHandshakeError) as rejected:
            await client.ws_connect(f"{url}/ws/demo/leader")
        assert rejected.value.status == 409
        follower = await client.ws_connect(f"{url}/ws/demo/follower")
        assert await leader.receive_json(timeout=2) == {"type": "ready"}
        assert await follower.receive_json(timeout=2) == {"type": "ready"}
        await leader.send_json({"type": "offer", "sdp": "fake-offer-for-relay-test"})
        assert (await follower.receive_json(timeout=2))["type"] == "offer"
        await follower.send_json({"type": "answer", "sdp": "fake-answer-for-relay-test"})
        assert (await leader.receive_json(timeout=2))["type"] == "answer"
        await leader.send_json({"type": "offer", "sdp": "repeated-offer"})
        await leader.receive(timeout=2)
        await follower.receive(timeout=2)
        await leader.close()
        await follower.receive(timeout=2)
        await follower.close()
        async with asyncio.timeout(2):
            while app[ROOMS]:
                await asyncio.sleep(0.01)


async def test_out_of_order_answer_ends_session(signaling_server):
    url, app = signaling_server
    async with ClientSession(headers={"Authorization": f"Bearer {TOKEN}"}) as client:
        leader = await client.ws_connect(f"{url}/ws/order/leader")
        follower = await client.ws_connect(f"{url}/ws/order/follower")
        await leader.receive_json(timeout=2)
        await follower.receive_json(timeout=2)
        await follower.send_json({"type": "answer", "sdp": "answer-first"})
        await follower.receive(timeout=2)
        await leader.receive(timeout=2)
        await leader.close()
        await follower.close()
    assert "order" not in app[ROOMS]


async def test_room_reusable_after_session_cleanup(signaling_server):
    url, app = signaling_server
    async with ClientSession(headers={"Authorization": f"Bearer {TOKEN}"}) as client:
        leader = await client.ws_connect(f"{url}/ws/reuse/leader")
        await leader.close()


async def test_shutdown_clears_and_closes_active_rooms(signaling_server):
    url, app = signaling_server
    async with ClientSession(headers={"Authorization": f"Bearer {TOKEN}"}) as client:
        leader = await client.ws_connect(f"{url}/ws/shutdown/leader")
        follower = await client.ws_connect(f"{url}/ws/shutdown/follower")
        await leader.receive_json(timeout=2)
        await follower.receive_json(timeout=2)
        await shutdown(app)
        assert not app[ROOMS]
        assert (await leader.receive(timeout=1)).type.name == "CLOSE"
        assert (await follower.receive(timeout=1)).type.name == "CLOSE"
        await leader.close()
        await follower.close()
        async with asyncio.timeout(2):
            while "reuse" in app[ROOMS]:
                await asyncio.sleep(0.01)
        leader = await client.ws_connect(f"{url}/ws/reuse/leader")
        await leader.close()


async def test_malformed_signal_ends_session(signaling_server):
    url, _ = signaling_server
    async with ClientSession(headers={"Authorization": f"Bearer {TOKEN}"}) as client:
        leader = await client.ws_connect(f"{url}/ws/bad/leader")
        follower = await client.ws_connect(f"{url}/ws/bad/follower")
        await leader.receive_json(timeout=2)
        await follower.receive_json(timeout=2)
        await leader.send_str("null")
        await leader.receive(timeout=2)
        await follower.receive(timeout=2)
        await leader.close()
        await follower.close()


@pytest.mark.parametrize(
    "payload",
    [
        {"type": "offer"},
        {"type": "offer", "sdp": None},
        {"type": "offer", "sdp": 3},
        {"type": "offer", "sdp": ""},
        {"type": "offer", "sdp": "x" * 65537},
    ],
)
async def test_invalid_sdp_metadata_closes_room(signaling_server, payload):
    url, app = signaling_server
    async with ClientSession(headers={"Authorization": f"Bearer {TOKEN}"}) as client:
        leader = await client.ws_connect(f"{url}/ws/invalid-sdp/leader")
        follower = await client.ws_connect(f"{url}/ws/invalid-sdp/follower")
        await leader.receive_json(timeout=2)
        await follower.receive_json(timeout=2)
        await leader.send_json(payload)
        await leader.receive(timeout=2)
        await follower.receive(timeout=2)
        await leader.close()
        await follower.close()
    assert "invalid-sdp" not in app[ROOMS]


async def test_malformed_signal_retires_room_before_slow_close(signaling_server, monkeypatch):
    from aiohttp import web

    url, app = signaling_server
    original_close = web.WebSocketResponse.close
    stalled = asyncio.Event()

    async def uncooperative_close(ws, *args, **kwargs):
        if kwargs.get("message") == b"Invalid signaling message":
            await original_close(ws, *args, **kwargs)
            stalled.set()
            await asyncio.Event().wait()
        return await original_close(ws, *args, **kwargs)

    monkeypatch.setattr(web.WebSocketResponse, "close", uncooperative_close)
    async with ClientSession(headers={"Authorization": f"Bearer {TOKEN}"}) as client:
        offender = await client.ws_connect(f"{url}/ws/slow/leader")
        companion = await client.ws_connect(f"{url}/ws/slow/follower")
        await offender.receive_json(timeout=2)
        await companion.receive_json(timeout=2)
        await offender.send_str("null")
        await asyncio.wait_for(stalled.wait(), 1)
        await companion.receive(timeout=1)
        async with asyncio.timeout(1):
            while "slow" in app[ROOMS]:
                await asyncio.sleep(0.01)
        replacement = await client.ws_connect(f"{url}/ws/slow/leader")
        await replacement.close()
        await offender.close()
        await companion.close()
