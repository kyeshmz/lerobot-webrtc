"""Connection URI policy shared by standalone commands and SDK configuration."""

import argparse
from urllib.parse import urlparse


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
