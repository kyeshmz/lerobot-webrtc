"""Generate short-lived Cloudflare TURN credentials and launch a finite bridge endpoint."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import re
import sys
from dataclasses import dataclass
from urllib.parse import urlsplit

import aiohttp

from lerobot_webrtc.cli import parser as bridge_parser
from lerobot_webrtc.cli import stun_url, turn_url

API_BASE = "https://rtc.live.cloudflare.com"
MAX_RESPONSE = 65536


class TurnSetupError(ValueError):
    """A safe-to-display configuration error; never contains provider response bodies."""


@dataclass(frozen=True)
class TurnCredentials:
    url: str
    username: str
    password: str
    stun: str | None


async def request_ice_servers(key_id: str, api_token: str, ttl: int) -> dict:
    if type(ttl) is not int or not 120 <= ttl <= 3600:
        raise TurnSetupError("This example requires a TTL between 120 and 3600 seconds")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", key_id):
        raise TurnSetupError("Set TURN_KEY_ID to a valid Cloudflare TURN key ID")
    if not api_token or any(not 33 <= ord(char) <= 126 for char in api_token):
        raise TurnSetupError("Set TURN_KEY_API_TOKEN to the TURN key's server-side API token")
    path = f"/v1/turn/keys/{key_id}/credentials/generate-ice-servers"
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
        async with session.post(
            API_BASE + path,
            headers={"Authorization": f"Bearer {api_token}"},
            json={"ttl": ttl},
            allow_redirects=False,
        ) as response:
            if not 200 <= response.status < 300:
                raise TurnSetupError(
                    f"Cloudflare TURN credential request failed (HTTP {response.status})"
                )
            body = bytearray()
            async for chunk in response.content.iter_chunked(8192):
                body.extend(chunk)
                if len(body) > MAX_RESPONSE:
                    raise TurnSetupError(
                        "Cloudflare TURN response exceeded the example's size limit"
                    )
    try:
        data = json.loads(body)
    except (ValueError, UnicodeError) as exc:
        raise TurnSetupError("Cloudflare TURN returned invalid JSON") from exc
    if not isinstance(data, dict) or not isinstance(data.get("iceServers"), list):
        raise TurnSetupError("Cloudflare TURN response must contain an iceServers array")
    return data


def select_credentials(data: dict, transport: str) -> TurnCredentials:
    if transport not in {"udp", "tcp", "tls"}:
        raise TurnSetupError("Choose udp, tcp, or tls")
    servers = data.get("iceServers")
    if not isinstance(servers, list):
        raise TurnSetupError("Cloudflare TURN response must contain an iceServers array")
    candidates = []
    stun = None
    for server in servers:
        if not isinstance(server, dict):
            raise TurnSetupError("Invalid ICE server entry")
        urls = server.get("urls")
        urls = [urls] if isinstance(urls, str) else urls
        if not isinstance(urls, list) or any(not isinstance(url, str) for url in urls):
            raise TurnSetupError("ICE server URLs must be strings")
        for url in urls:
            scheme, _, address = url.partition(":")
            try:
                authority, _, query = address.partition("?")
                port = urlsplit("//" + authority).port
                if port == 53:
                    continue
                if scheme == "stun":
                    stun = stun or stun_url(url)
                    continue
                if scheme not in {"turn", "turns"}:
                    continue
                turn_url(url)
            except (ValueError, argparse.ArgumentTypeError) as exc:
                raise TurnSetupError("Provider returned an incompatible ICE URL") from exc
            actual = "tls" if scheme == "turns" else "tcp" if query == "transport=tcp" else "udp"
            if actual != transport:
                continue
            username, password = server.get("username"), server.get("credential")
            if any(
                not isinstance(value, str) or not value or len(value) > 4096 or "\0" in value
                for value in (username, password)
            ):
                raise TurnSetupError("TURN entry is missing usable temporary credentials")
            primary = 443 if transport == "tls" else 3478
            candidates.append((port not in {None, primary}, url, username, password))
    if not candidates:
        raise TurnSetupError("Provider returned no compatible TURN URL for the requested transport")
    _, url, username, password = min(candidates)
    return TurnCredentials(url, username, password, stun)


def launch_credentials(endpoint: list[str], credentials: TurnCredentials, env: dict) -> None:
    child_env = dict(env)
    child_env.pop("TURN_KEY_API_TOKEN", None)
    child_env.pop("TURN_KEY_ID", None)
    child_env["LEROBOT_TURN_USERNAME"] = credentials.username
    child_env["LEROBOT_TURN_PASSWORD"] = credentials.password
    command = [sys.executable, "-m", "lerobot_webrtc", *endpoint, "--turn", credentials.url]
    if credentials.stun:
        command += ["--stun", credentials.stun]
    os.execve(sys.executable, command, child_env)


def main(argv: list[str] | None = None) -> int:
    root = argparse.ArgumentParser(description=__doc__)
    root.add_argument("--ttl", type=int, default=600, help="Credential lifetime, 120–3600 seconds")
    root.add_argument("--transport", choices=("udp", "tcp", "tls"), default="udp")
    root.add_argument("endpoint", nargs=argparse.REMAINDER, help="-- leader|follower|bench ...")
    args = root.parse_args(argv)
    endpoint = args.endpoint[1:] if args.endpoint[:1] == ["--"] else args.endpoint
    if not endpoint or endpoint[0] not in {"leader", "follower", "bench"}:
        root.error("Supply -- followed by a leader, follower, or bench command")
    parsed = bridge_parser().parse_args(endpoint)
    if parsed.turn:
        root.error("This helper supplies --turn; do not pass a second provider URL")
    if parsed.duration is None or not math.isfinite(parsed.duration):
        root.error("This example requires a finite --duration")
    if not 120 <= args.ttl <= 3600 or parsed.duration + 90 > args.ttl:
        root.error("Use ttl 120–3600 and leave at least 90 seconds beyond the endpoint duration")
    if len(os.environ.get("LEROBOT_WEBRTC_TOKEN", "")) < 16:
        root.error("Set the shared LEROBOT_WEBRTC_TOKEN before requesting credentials")
    try:
        data = asyncio.run(
            request_ice_servers(
                os.environ.get("TURN_KEY_ID", ""),
                os.environ.get("TURN_KEY_API_TOKEN", ""),
                args.ttl,
            )
        )
        credentials = select_credentials(data, args.transport)
        launch_credentials(endpoint, credentials, os.environ)
        return 0
    except TurnSetupError as exc:
        print(f"TURN setup failed: {exc}", file=sys.stderr)
    except (OSError, aiohttp.ClientError, TimeoutError):
        print(
            "TURN setup or endpoint launch failed; check network and local configuration",
            file=sys.stderr,
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
