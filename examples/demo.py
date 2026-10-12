"""Run a finite, hardware-free local WebRTC control demonstration."""

from __future__ import annotations

import argparse
import os
import re
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
from queue import Empty, Queue
from urllib.error import URLError
from urllib.request import urlopen

SERVER_STARTUP_TIMEOUT = 10
ENDPOINT_STARTUP_TIMEOUT = 10
ENDPOINT_SHUTDOWN_TIMEOUT = 5
CHILD_CLEANUP_TIMEOUT = 3
READER_CLEANUP_TIMEOUT = 1


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_for_server(child: subprocess.Popen[str], port: int, deadline: float) -> None:
    health_url = f"http://127.0.0.1:{port}/health"
    while time.monotonic() < deadline:
        if child.poll() is not None:
            raise RuntimeError(f"Signaling server exited with status {child.returncode}")
        try:
            with urlopen(health_url, timeout=0.2) as response:
                if response.status == 200:
                    return
        except (OSError, URLError):
            time.sleep(0.05)
    raise RuntimeError("Signaling server did not become ready before timeout")


def drain(child: subprocess.Popen[str], lines: Queue[str]) -> None:
    assert child.stdout is not None
    for line in child.stdout:
        lines.put(line)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--duration", type=float, default=5, help="connected run duration (3–30 seconds)"
    )
    args = parser.parse_args()
    if not 3 <= args.duration <= 30:
        parser.error("--duration must be between 3 and 30 seconds")
    if os.name != "posix":
        parser.error("the finite demo currently requires POSIX SIGINT process handling")

    port = free_port()
    token = secrets.token_urlsafe(32)
    env = os.environ.copy()
    env["LEROBOT_WEBRTC_TOKEN"] = token
    python = sys.executable
    commands = [
        [python, "-m", "lerobot_webrtc", "serve", "--host", "127.0.0.1", "--port", str(port)],
        [
            python,
            "-m",
            "lerobot_webrtc",
            "follower",
            "--simulate",
            "--room",
            "demo",
            "--signaling",
            f"ws://127.0.0.1:{port}",
        ],
        [
            python,
            "-m",
            "lerobot_webrtc",
            "leader",
            "--simulate",
            "--room",
            "demo",
            "--signaling",
            f"ws://127.0.0.1:{port}",
        ],
    ]
    children: list[subprocess.Popen[str]] = []
    lines: Queue[str] = Queue()
    readers: list[threading.Thread] = []

    def start(command: list[str]) -> subprocess.Popen[str]:
        child = subprocess.Popen(
            command, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1
        )
        children.append(child)
        reader = threading.Thread(target=drain, args=(child, lines), daemon=True)
        reader.start()
        readers.append(reader)
        return child

    deadline = time.monotonic() + SERVER_STARTUP_TIMEOUT
    try:
        start(commands[0])
        wait_for_server(children[0], port, deadline)
        start(commands[1])
        start(commands[2])
        startup_deadline = time.monotonic() + ENDPOINT_STARTUP_TIMEOUT
        run_deadline = startup_deadline
        control_started = False
        applied = 0
        while time.monotonic() < run_deadline:
            for child in children:
                if child.poll() is not None:
                    raise RuntimeError(f"Demo child exited early with status {child.returncode}")
            try:
                line = lines.get(timeout=min(0.1, max(0, run_deadline - time.monotonic())))
                match = re.search(r"Follower applied ([1-9][0-9]*) commands", line)
                if match:
                    applied = max(applied, int(match.group(1)))
                    if not control_started:
                        control_started = True
                        run_deadline = time.monotonic() + args.duration
            except Empty:
                pass
        if not control_started or applied <= 0:
            raise RuntimeError("Endpoints did not establish control before startup timeout")
        # Ask endpoints to shut down gracefully; the parent, not endpoint timers,
        # controls the finite run so one peer cannot outlast and trip the other.
        for child in children[1:]:
            if child.poll() is None:
                try:
                    child.send_signal(signal.SIGINT)
                except ProcessLookupError:
                    pass
        for child in children[1:]:
            child.wait(timeout=ENDPOINT_SHUTDOWN_TIMEOUT)
            if child.returncode:
                raise RuntimeError(
                    f"Endpoint exited during shutdown with status {child.returncode}"
                )
        print(
            "Hardware-free WebRTC demo completed: simulated follower control "
            f"ran for {args.duration:g}s."
        )
        return 0
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print(f"Demo failed: {exc}", file=sys.stderr)
        return 1
    finally:
        for child in children:
            if child.poll() is None:
                try:
                    child.send_signal(signal.SIGINT)
                except ProcessLookupError:
                    # The child exited between poll() and signal delivery.
                    pass
        for child in children:
            try:
                child.wait(timeout=CHILD_CLEANUP_TIMEOUT)
            except subprocess.TimeoutExpired:
                child.kill()
                try:
                    child.wait(timeout=CHILD_CLEANUP_TIMEOUT)
                except subprocess.TimeoutExpired:
                    pass
        for reader in readers:
            reader.join(timeout=READER_CLEANUP_TIMEOUT)
        # Child logs are intentionally drained but never echoed (they can contain tokens).


if __name__ == "__main__":
    raise SystemExit(main())
