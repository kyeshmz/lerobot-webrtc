"""WebRTC connection with full-SDP signaling and a non-retransmitting channel."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from urllib.parse import urlparse

from aiohttp import ClientSession, WSMsgType
from aiortc import (
    RTCConfiguration,
    RTCDataChannel,
    RTCIceServer,
    RTCPeerConnection,
    RTCSessionDescription,
)

logger = logging.getLogger(__name__)
LABEL = "lerobot-actions-v1"
MAX_SDP_SIZE = 65536


def _signaled_sdp(data: object) -> str:
    if not isinstance(data, dict):
        raise ValueError("Invalid signaling SDP")
    sdp = data.get("sdp")
    if not isinstance(sdp, str) or not 0 < len(sdp) <= MAX_SDP_SIZE:
        raise ValueError("Invalid signaling SDP")
    return sdp


class Peer:
    def __init__(
        self,
        role: str,
        signaling: str,
        room: str,
        token: str,
        ice_servers: list[RTCIceServer] | None = None,
    ):
        self.role = role
        self.url = f"{signaling.rstrip('/')}/ws/{room}/{role}"
        self.token = token
        self.pc = RTCPeerConnection(RTCConfiguration(iceServers=ice_servers or []))
        self.channel: RTCDataChannel | None = None
        self.opened = asyncio.Event()
        self.closed = asyncio.Event()
        self.error: Exception | None = None
        self.closing = False
        self.on_message: Callable[[str | bytes], None] = lambda message: None
        self.session: ClientSession | None = None
        self.signal_task: asyncio.Task | None = None
        self._connect_started = False
        self._close_task: asyncio.Task | None = None

        @self.pc.on("connectionstatechange")
        def state_changed():
            logger.info("WebRTC: %s", self.pc.connectionState)
            if self.pc.connectionState in {"failed", "closed", "disconnected"}:
                self.fail(ConnectionError(f"WebRTC {self.pc.connectionState}"))

        if role == "leader":
            self.attach(self.pc.createDataChannel(LABEL, ordered=False, maxRetransmits=0))
        else:

            @self.pc.on("datachannel")
            def datachannel(channel):
                self.attach(channel)

    def fail(self, error: Exception) -> None:
        if not self.closing and not self.closed.is_set():
            self.error = error
            self.closed.set()

    def attach(self, channel: RTCDataChannel) -> None:
        if (
            self.channel is not None
            or channel.label != LABEL
            or channel.ordered
            or channel.maxRetransmits != 0
            or channel.maxPacketLifeTime is not None
        ):
            self.fail(ValueError("Unexpected WebRTC data channel"))
            return
        self.channel = channel

        @channel.on("open")
        def opened():
            self.opened.set()

        @channel.on("close")
        def closed():
            self.fail(ConnectionError("Control data channel closed"))

        @channel.on("message")
        def message(data):
            if not self.closed.is_set():
                try:
                    self.on_message(data)
                except Exception as exc:
                    self.fail(exc)

        # The receiver's datachannel event may arrive after its open transition.
        if channel.readyState == "open":
            self.opened.set()

    def send(self, message: str) -> bool:
        channel = self.channel
        if self.closing or self.closed.is_set() or channel is None or channel.readyState != "open":
            return False
        # Drop rather than enqueue another command on a congested transport.
        if channel.bufferedAmount > 0:
            return False
        channel.send(message)
        return True

    async def connect(self, timeout: float = 45) -> None:
        if self._connect_started or self.closing or self.closed.is_set():
            raise RuntimeError("Peer connections are single-use")
        self._connect_started = True
        self.signal_task = asyncio.create_task(self.signal(), name="signaling")
        opened = asyncio.create_task(self.opened.wait())
        closed = asyncio.create_task(self.closed.wait())
        try:
            async with asyncio.timeout(timeout):
                await asyncio.wait({opened, closed}, return_when=asyncio.FIRST_COMPLETED)
            if self.closed.is_set():
                raise self.error or ConnectionError("Connection ended")
        except BaseException:
            await self.close()
            raise
        finally:
            for task in (opened, closed):
                task.cancel()
            await asyncio.gather(opened, closed, return_exceptions=True)

    async def signal(self) -> None:
        try:
            if urlparse(self.url).scheme != "wss":
                logger.warning("Signaling is unencrypted; use WSS or a trusted private network")
            async with ClientSession() as session:
                self.session = session
                async with session.ws_connect(
                    self.url,
                    headers={"Authorization": f"Bearer {self.token}"},
                    heartbeat=10,
                    max_msg_size=131072,
                ) as ws:
                    negotiated = False
                    async for message in ws:
                        if message.type != WSMsgType.TEXT:
                            break
                        data = json.loads(message.data)
                        if not isinstance(data, dict):
                            raise ValueError("Invalid signaling response")
                        kind = data.get("type")
                        if kind == "ready" and self.role == "leader" and not negotiated:
                            negotiated = True
                            await self.pc.setLocalDescription(await self.pc.createOffer())
                            await ws.send_json(
                                {"type": "offer", "sdp": self.pc.localDescription.sdp}
                            )
                        elif kind == "ready" and self.role == "follower":
                            continue
                        elif kind == "offer" and self.role == "follower" and not negotiated:
                            negotiated = True
                            await self.pc.setRemoteDescription(
                                RTCSessionDescription(_signaled_sdp(data), kind)
                            )
                            await self.pc.setLocalDescription(await self.pc.createAnswer())
                            await ws.send_json(
                                {"type": "answer", "sdp": self.pc.localDescription.sdp}
                            )
                        elif kind == "answer" and self.role == "leader" and negotiated:
                            if getattr(self, "answer_received", False):
                                raise ValueError("Repeated signaling answer")
                            self.answer_received = True
                            await self.pc.setRemoteDescription(
                                RTCSessionDescription(_signaled_sdp(data), kind)
                            )
                        else:
                            raise ValueError("Unexpected signaling response")
                    self.fail(ConnectionError("Signaling session ended; restart both peers"))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.fail(exc)

    async def close(self) -> None:
        if self._close_task is None:
            self.closing = True
            self.closed.set()
            self._close_task = asyncio.create_task(self._close_owned(), name="peer-close")
        await asyncio.shield(self._close_task)

    async def _close_owned(self) -> None:
        if self.signal_task is not None and self.signal_task is not asyncio.current_task():
            self.signal_task.cancel()
            await asyncio.gather(self.signal_task, return_exceptions=True)
        await self.pc.close()
