# /// script
# requires-python = ">=3.12"
# dependencies = ["livekit==1.1.21"]
# ///
"""Finite LiveKit data-packet probe. No robot driver or bridge motor protocol is used."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sys
from urllib.parse import urlsplit

TOPIC = "lerobot-webrtc-network-probe-v1"


def connection_settings(env):
    url, token = env.get("LIVEKIT_URL", ""), env.get("LIVEKIT_TOKEN", "")
    try:
        parsed = urlsplit(url)
        port = parsed.port
        safe_scheme = parsed.scheme == "wss" or (
            parsed.scheme == "ws" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        )
        if (
            not safe_scheme
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or (port is not None and not 1 <= port <= 65535)
        ):
            raise ValueError
    except ValueError as exc:
        raise ValueError(
            "Set LIVEKIT_URL to wss://... (ws:// is allowed only on localhost)"
        ) from exc
    if not token or "\0" in token:
        raise ValueError("Set LIVEKIT_TOKEN to a short-lived, room-scoped participant token")
    return url, token


async def run_probe(role: str, peer: str, duration: float, url: str, token: str) -> dict:
    from livekit import rtc

    room = rtc.Room()
    received = set()
    disconnected = asyncio.Event()

    @room.on("data_received")
    def on_data(packet):
        participant = packet.participant
        if (
            packet.topic != TOPIC
            or participant is None
            or participant.identity != peer
            or len(packet.data) > 512
        ):
            return
        try:
            data = json.loads(packet.data)
        except (ValueError, UnicodeError):
            return
        if (
            isinstance(data, dict)
            and data.get("kind") == "network-probe"
            and type(data.get("seq")) is int
            and 0 <= data["seq"] < 5
        ):
            received.add(data["seq"])

    @room.on("disconnected")
    def on_disconnected(*args):
        disconnected.set()

    submitted = 0
    try:
        async with asyncio.timeout(45):
            await room.connect(
                url, token, rtc.RoomOptions(auto_subscribe=False, connect_timeout=10)
            )
            if role == "send":
                async with asyncio.timeout(duration):
                    while not any(p.identity == peer for p in room.remote_participants.values()):
                        if disconnected.is_set():
                            raise ConnectionError(
                                "LiveKit room disconnected before the peer joined"
                            )
                        await asyncio.sleep(0.05)
                for sequence in range(5):
                    await room.local_participant.publish_data(
                        json.dumps({"kind": "network-probe", "seq": sequence}),
                        reliable=False,
                        destination_identities=[peer],
                        topic=TOPIC,
                    )
                    submitted += 1
                    await asyncio.sleep(0.1)
            else:
                await asyncio.sleep(duration)
            if disconnected.is_set():
                raise ConnectionError("LiveKit room disconnected during the probe")
        return {
            "role": role,
            "messages_submitted": submitted,
            "unique_messages_received": len(received),
            "delivery_verified_by_sender": False,
            "unreliable": True,
            "motor_access": False,
        }
    finally:
        await asyncio.wait_for(room.disconnect(), timeout=10)


def main(argv=None) -> int:
    root = argparse.ArgumentParser(description=__doc__)
    root.add_argument("role", choices=("send", "receive"))
    root.add_argument("--peer", required=True, help="Other participant's assigned identity")
    root.add_argument(
        "--duration", type=float, default=10, help="Receive/peer wait time, 1–30 seconds"
    )
    args = root.parse_args(argv)
    if not math.isfinite(args.duration) or not 1 <= args.duration <= 30:
        root.error("Use a duration between 1 and 30 seconds")
    try:
        url, token = connection_settings(os.environ)
        result = asyncio.run(run_probe(args.role, args.peer, args.duration, url, token))
    except (ValueError, ImportError):
        print(
            "Probe setup failed; use uv run --script and set the documented URL/token variables",
            file=sys.stderr,
        )
        return 1
    except Exception:
        print(
            "LiveKit probe failed; check participant grants, room, peer identity, and connectivity",
            file=sys.stderr,
        )
        return 1
    print(json.dumps(result))
    return 0 if args.role == "send" or result["unique_messages_received"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
