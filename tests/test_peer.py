import asyncio
from types import SimpleNamespace

import pytest

from lerobot_webrtc.peer import LABEL, Peer, _signaled_sdp


@pytest.mark.asyncio
async def test_connect_timeout_closes_peer():
    peer = Peer("leader", "ws://127.0.0.1:1", "timeout", "secret")

    async def wait_for_signal():
        await asyncio.Event().wait()

    peer.signal = wait_for_signal
    with pytest.raises(TimeoutError):
        await peer.connect(timeout=0.05)
    assert peer.closing
    assert peer.pc.connectionState == "closed"
    await peer.close()


@pytest.mark.asyncio
async def test_connect_cancellation_closes_peer():
    peer = Peer("leader", "ws://127.0.0.1:1", "cancel", "secret")

    async def wait_for_signal():
        await asyncio.Event().wait()

    peer.signal = wait_for_signal
    task = asyncio.create_task(peer.connect(timeout=10))
    await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert peer.closing
    assert peer.pc.connectionState == "closed"


@pytest.mark.asyncio
async def test_close_is_shared_and_cancelled_waiter_does_not_abandon_cleanup():
    peer = Peer("leader", "ws://127.0.0.1:1", "close-race", "secret")
    entered = asyncio.Event()
    release = asyncio.Event()
    original_close = peer.pc.close

    async def slow_close():
        entered.set()
        await release.wait()
        await original_close()

    peer.pc.close = slow_close
    first = asyncio.create_task(peer.close())
    await entered.wait()
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert peer.closed.is_set()
    assert not peer.send("ignored")
    second = asyncio.create_task(peer.close())
    release.set()
    await asyncio.wait_for(second, 1)
    assert peer.pc.connectionState == "closed"
    await peer.close()


@pytest.mark.asyncio
async def test_connect_is_single_use_after_close_and_while_connecting():
    peer = Peer("leader", "ws://127.0.0.1:1", "single-use", "secret")
    started = asyncio.Event()

    async def wait_for_signal():
        started.set()
        await asyncio.Event().wait()

    peer.signal = wait_for_signal
    connecting = asyncio.create_task(peer.connect(timeout=10))
    await started.wait()
    with pytest.raises(RuntimeError, match="single-use"):
        await peer.connect(timeout=1)
    connecting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await connecting
    with pytest.raises(RuntimeError, match="single-use"):
        await peer.connect(timeout=1)


@pytest.mark.parametrize(
    "metadata",
    [None, {}, {"sdp": None}, {"sdp": 1}, {"sdp": ""}, {"sdp": "x" * 65537}],
)
def test_rejects_malformed_sdp_metadata(metadata):
    with pytest.raises(ValueError, match="Invalid signaling SDP"):
        _signaled_sdp(metadata)


@pytest.mark.parametrize(
    "channel",
    [
        SimpleNamespace(label="wrong", ordered=False, maxRetransmits=0, maxPacketLifeTime=None),
        SimpleNamespace(label=LABEL, ordered=True, maxRetransmits=0, maxPacketLifeTime=None),
        SimpleNamespace(label=LABEL, ordered=False, maxRetransmits=1, maxPacketLifeTime=None),
        SimpleNamespace(label=LABEL, ordered=False, maxRetransmits=0, maxPacketLifeTime=1),
    ],
)
def test_rejects_unreliable_channel_mismatch(channel):
    peer = Peer("follower", "ws://127.0.0.1:1", "invalid-channel", "secret")
    peer.attach(channel)
    assert isinstance(peer.error, ValueError)
    assert peer.closed.is_set()


async def test_native_and_standalone_channels_do_not_cross_activate():
    from lerobot_webrtc.native_protocol import NATIVE_LABEL

    peer = Peer("follower", "ws://127.0.0.1:1", "native", "secret", channel_label=NATIVE_LABEL)
    try:
        peer.attach(
            SimpleNamespace(label=LABEL, ordered=False, maxRetransmits=0, maxPacketLifeTime=None)
        )
        assert peer.closed.is_set()
        assert not peer.opened.is_set()
        assert peer.channel is None
    finally:
        await peer.close()
