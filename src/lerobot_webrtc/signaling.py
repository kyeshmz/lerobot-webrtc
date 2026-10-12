"""Authenticated SDP relay. Joint commands never pass through this server."""

from __future__ import annotations

import asyncio
import hmac
import json
import re

from aiohttp import WSMsgType, web

ROOMS = web.AppKey("rooms", dict)
LOCK = web.AppKey("lock", asyncio.Lock)
TOKEN = web.AppKey("token", str)
SESSIONS = web.AppKey("sessions", dict)
TEARDOWN_TIMEOUT = 1.0


async def _close_sockets(sockets: list[web.WebSocketResponse], message: bytes) -> None:
    async def close_one(ws: web.WebSocketResponse) -> None:
        try:
            async with asyncio.timeout(TEARDOWN_TIMEOUT):
                await ws.close(code=1001, message=message)
        except (TimeoutError, Exception):
            pass

    await asyncio.gather(*(close_one(ws) for ws in sockets), return_exceptions=True)


def create_app(token: str) -> web.Application:
    if len(token) < 16:
        raise ValueError("Signaling token must have at least 16 characters")
    app = web.Application()
    app[ROOMS] = {}
    app[LOCK] = asyncio.Lock()
    app[TOKEN] = token
    app[SESSIONS] = {}
    app.router.add_get("/health", health)
    app.router.add_get("/ws/{room}/{role}", websocket)
    app.on_shutdown.append(shutdown)
    return app


async def health(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


async def shutdown(app: web.Application) -> None:
    sockets = [ws for room in app[ROOMS].values() for ws in room.values()]
    app[ROOMS].clear()
    app[SESSIONS].clear()
    await _close_sockets(sockets, b"Server shutting down")


async def websocket(request: web.Request) -> web.WebSocketResponse:
    app = request.app
    supplied = request.headers.get("Authorization", "")
    if not hmac.compare_digest(supplied.encode(), f"Bearer {app[TOKEN]}".encode()):
        raise web.HTTPUnauthorized(text="Invalid signaling token")
    room_id, role = request.match_info["room"], request.match_info["role"]
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", room_id) or role not in {"leader", "follower"}:
        raise web.HTTPBadRequest(text="Invalid room or role")
    other_role = "follower" if role == "leader" else "leader"
    ws = web.WebSocketResponse(heartbeat=10, max_msg_size=131072)
    async with app[LOCK]:
        rooms = app[ROOMS]
        if room_id not in rooms and len(rooms) >= 32:
            raise web.HTTPServiceUnavailable(text="Room limit reached")
        room = rooms.setdefault(room_id, {})
        if role in room:
            raise web.HTTPConflict(text="This room already has that role")
        # Registration and ready notifications are atomic with respect to joining.
        try:
            await ws.prepare(request)
        except BaseException:
            if not room:
                rooms.pop(room_id, None)
            raise
        room[role] = ws
        session = app[SESSIONS].setdefault(room_id, {"offer": False, "answer": False})
    try:
        if other_role in room:
            await asyncio.gather(*(member.send_json({"type": "ready"}) for member in room.values()))
        async for message in ws:
            if message.type != WSMsgType.TEXT:
                break
            try:
                data = json.loads(message.data)
                expected = "offer" if role == "leader" else "answer"
                if (
                    not isinstance(data, dict)
                    or data.get("type") != expected
                    or not isinstance(data.get("sdp"), str)
                    or not 0 < len(data["sdp"]) <= 65536
                ):
                    raise ValueError("Invalid SDP message")
                if expected == "offer":
                    if session["offer"]:
                        raise ValueError("Repeated offer")
                    session["offer"] = True
                else:
                    if not session["offer"] or session["answer"]:
                        raise ValueError("Out-of-order or repeated answer")
                    session["answer"] = True
                other = room.get(other_role)
                if other is None or other.closed:
                    raise ValueError("Other peer is absent")
                await other.send_json({"type": expected, "sdp": data["sdp"]})
            except (ValueError, TypeError):
                # Retire and notify the companion before waiting on the offender's
                # close handshake. The old room identity cannot affect its reuse.
                async with app[LOCK]:
                    if app[ROOMS].get(room_id) is room:
                        app[ROOMS].pop(room_id, None)
                        app[SESSIONS].pop(room_id, None)
                        peers = list(room.values())
                    else:
                        peers = []
                await _close_sockets(peers, b"Invalid signaling message")
                break
    finally:
        async with app[LOCK]:
            # A session is single-use: end BOTH roles rather than silently repairing it.
            if app[ROOMS].get(room_id) is room:
                app[ROOMS].pop(room_id)
                app[SESSIONS].pop(room_id, None)
                peers = list(room.values())
            else:
                peers = []
        await _close_sockets(peers, b"Session ended")
    return ws
