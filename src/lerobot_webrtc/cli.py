from __future__ import annotations

import argparse
import asyncio
import logging
import math
import os
import re
from urllib.parse import urlparse

from aiohttp import web
from aiortc import RTCIceServer

from .arms import AsyncArm, LeRobotArm, SimulatedArm
from .peer import Peer
from .runtime import run_control
from .signaling import create_app


def positive(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("Expected a finite positive number")
    return number


def server_port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Expected a port between 1 and 65535") from exc
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("Expected a port between 1 and 65535")
    return port


def signaling_url(value: str) -> str:
    try:
        url = urlparse(value)
        port = url.port
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Invalid signaling URL or port") from exc
    if (
        url.scheme not in {"ws", "wss"}
        or not url.hostname
        or url.username is not None
        or url.password is not None
        or url.query
        or url.fragment
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise argparse.ArgumentTypeError(
            "Signaling must be ws:// or wss:// without userinfo, query, or fragment"
        )
    return value


def ice_url(value: str, schemes: set[str], label: str) -> str:
    try:
        scheme, rest = value.split(":", 1)
        if scheme not in schemes or not rest or rest.startswith("//"):
            raise ValueError("invalid URI scheme or authority")
        authority, separator, query = rest.partition("?")
        if "#" in authority or "@" in authority or "/" in authority or "\\" in authority:
            raise ValueError("invalid URI authority")
        if separator and (label != "TURN" or query not in {"transport=udp", "transport=tcp"}):
            raise ValueError("unsupported URI option")
        if scheme == "turns" and separator and query != "transport=tcp":
            raise ValueError("secure TURN requires TCP")
        host, colon, port_text = authority.rpartition(":")
        if not colon:
            host, port_text = authority, ""
        if not host or any(c.isspace() for c in host) or ":" in host:
            raise ValueError("invalid host")
        if port_text:
            if not port_text.isascii() or not port_text.isdecimal():
                raise ValueError("invalid port")
            port = int(port_text)
            if not 1 <= port <= 65535:
                raise ValueError("invalid port")
        elif colon:
            raise ValueError("empty port")
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"Invalid {label} URL or port") from exc
    return value


def stun_url(value: str) -> str:
    return ice_url(value, {"stun"}, "STUN")


def turn_url(value: str) -> str:
    return ice_url(value, {"turn", "turns"}, "TURN")


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="LeRobot leader/follower teleoperation over WebRTC")
    root.add_argument("--verbose", action="store_true")
    sub = root.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="Run the authenticated SDP signaling server")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=server_port, default=8080)
    for role in ("leader", "follower", "calibrate"):
        command = sub.add_parser(role)
        command.add_argument("--robot", choices=("so101", "so100"), default="so101")
        command.add_argument("--port", help="Local serial port for this arm")
        command.add_argument(
            "--id", help="LeRobot calibration ID (must match during calibration/use)"
        )
        command.add_argument("--max-relative-target", type=positive, default=5.0)
        command.add_argument("--on-exit", choices=("hold", "torque-off"), default="hold")
        if role == "calibrate":
            command.add_argument("--role", choices=("leader", "follower"), required=True)
            continue
        command.add_argument(
            "--simulate", action="store_true", help="No hardware or LeRobot required"
        )
        command.add_argument("--signaling", type=signaling_url, default="ws://127.0.0.1:8080")
        command.add_argument("--room", default="arm")
        command.add_argument("--fps", type=positive, default=30.0)
        command.add_argument("--timeout", type=positive, default=0.3)
        command.add_argument(
            "--duration", type=positive, help="Exit after this many connected seconds"
        )
        command.add_argument(
            "--stun", action="append", type=stun_url, default=[], help="STUN URL; may be repeated"
        )
        command.add_argument(
            "--turn", type=turn_url, help="TURN URL, e.g. turn:relay.example.com:3478"
        )
        if role == "follower":
            command.add_argument(
                "--arm", action="store_true", help="Explicitly allow real motor motion"
            )
    return root


def calibrate(args) -> None:
    device = LeRobotArm(
        args.role, args.robot, args.port, args.id, args.max_relative_target, "torque-off"
    )
    try:
        device.calibrate()
    except BaseException as calibration_error:
        try:
            device.disconnect()
        except BaseException as cleanup_error:
            raise BaseExceptionGroup(
                "Calibration and torque-off disconnect both failed",
                [calibration_error, cleanup_error],
            ) from calibration_error
        raise
    else:
        # Do not re-enable torque by holding a pose after interactive calibration.
        device.disconnect()


async def endpoint(args, token: str) -> None:
    servers = [RTCIceServer(urls=url) for url in args.stun]
    if args.turn:
        servers.append(
            RTCIceServer(
                urls=args.turn,
                username=os.environ["LEROBOT_TURN_USERNAME"],
                credential=os.environ["LEROBOT_TURN_PASSWORD"],
            )
        )
    if args.simulate:
        device = SimulatedArm(args.max_relative_target)
    else:
        device = LeRobotArm(
            args.command, args.robot, args.port, args.id, args.max_relative_target, args.on_exit
        )
    arm = AsyncArm(device, args.command)
    peer = Peer(args.command, args.signaling, args.room, token, servers)
    await run_control(arm, peer, args.robot, args.fps, args.timeout, args.duration)


def main() -> None:
    root = parser()
    args = root.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Never enable aiohttp/aiortc DEBUG logs by default (SDP reveals IP addresses).
    token = os.environ.get("LEROBOT_WEBRTC_TOKEN", "")
    if args.command != "calibrate" and len(token) < 16:
        root.error("Set LEROBOT_WEBRTC_TOKEN to a shared secret of at least 16 characters")
    if args.command in {"leader", "follower", "calibrate"}:
        simulated = getattr(args, "simulate", False)
        if not simulated and (not args.port or not args.id):
            root.error("Real hardware requires --port and --id; calibrate using the same ID first")
    if args.command in {"leader", "follower"}:
        if args.command == "follower" and not args.simulate and not args.arm:
            root.error("Real follower motion requires explicit --arm")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", args.room):
            root.error("Room must be 1–64 letters, digits, underscores, or hyphens")
        url = urlparse(args.signaling)
        if url.scheme not in {"ws", "wss"} or not url.hostname or url.query or url.fragment:
            root.error("Signaling must be a ws:// or wss:// base URL without a query or fragment")
        if args.fps > 100 or args.timeout > 2 or args.timeout < 2 / args.fps:
            root.error("Use fps <= 100 and a timeout between two frame intervals and 2 seconds")
        if args.max_relative_target > 100:
            root.error("max-relative-target must be <= 100 normalized units")
        if args.turn and not all(
            os.environ.get(key) for key in ("LEROBOT_TURN_USERNAME", "LEROBOT_TURN_PASSWORD")
        ):
            root.error("TURN requires LEROBOT_TURN_USERNAME and LEROBOT_TURN_PASSWORD")
    try:
        if args.command == "serve":
            web.run_app(create_app(token), host=args.host, port=args.port, access_log=None)
        elif args.command == "calibrate":
            # Keep interactive LeRobot input on the main thread: asyncio's SIGINT
            # handler cancels its main task rather than interrupting input().
            calibrate(args)
        else:
            asyncio.run(endpoint(args, token))
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        logging.getLogger(__name__).error("Session stopped: %s", exc)
        if args.verbose:
            logging.getLogger(__name__).exception("Details")
        raise SystemExit(1) from exc
