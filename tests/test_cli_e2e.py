from __future__ import annotations

import io
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

import examples.demo as demo

ROOT = Path(__file__).resolve().parents[1]


def test_finite_demo_runs_real_cli_processes_and_redacts_exact_token(monkeypatch, capsys):
    secret = "exact-test-token-that-must-never-appear"
    monkeypatch.setattr(demo.secrets, "token_urlsafe", lambda _: secret)
    monkeypatch.setattr(sys, "argv", ["demo.py", "--duration", "3"])
    started = time.monotonic()
    assert demo.main() == 0
    # Follower progress is logged at two-second intervals: the three-second
    # requested run must begin only after that positive control evidence.
    assert time.monotonic() - started >= 5
    captured = capsys.readouterr()
    output = captured.out
    assert "simulated follower control" in output
    assert secret not in output
    assert secret not in captured.err


def test_demo_bounds_server_startup_failure_and_cleans_child(monkeypatch, capsys):
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        port = occupied.getsockname()[1]
        monkeypatch.setattr(demo, "free_port", lambda: port)
        monkeypatch.setattr(sys, "argv", ["demo.py", "--duration", "3"])
        children = []
        real_popen = demo.subprocess.Popen

        def record_child(*args, **kwargs):
            child = real_popen(*args, **kwargs)
            children.append(child)
            return child

        monkeypatch.setattr(demo.subprocess, "Popen", record_child)
        assert demo.main() == 1
        error = capsys.readouterr().err
        assert "did not become ready" in error or "exited" in error
        assert children
        assert all(child.poll() is not None for child in children)


def test_demo_rejects_unbounded_duration_quickly():
    result = subprocess.run(
        [sys.executable, "examples/demo.py", "--duration", "999"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=3,
        check=False,
    )
    assert result.returncode == 2
    assert "between 3 and 30" in result.stderr


class FakeChild:
    def __init__(self, output="", returncode=None, race=False, stubborn=False):
        self.stdout = io.StringIO(output)
        self.returncode = returncode
        self.race = race
        self.signals = 0
        self.killed = False
        self.stubborn = stubborn
        self.wait_calls = 0

    def poll(self):
        return self.returncode

    def send_signal(self, _signal):
        self.signals += 1
        if self.race:
            self.returncode = 0
            raise ProcessLookupError
        if not self.stubborn:
            self.returncode = 0

    def wait(self, timeout=None):
        assert timeout is not None
        self.wait_calls += 1
        if self.stubborn and not self.killed:
            raise subprocess.TimeoutExpired("fake-child", timeout)
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9


def setup_fake_demo(monkeypatch, children):
    monkeypatch.setattr(demo, "free_port", lambda: 45678)
    monkeypatch.setattr(demo, "wait_for_server", lambda *_: None)
    monkeypatch.setattr(demo.subprocess, "Popen", lambda *_a, **_k: children.pop(0))
    monkeypatch.setattr(demo, "ENDPOINT_STARTUP_TIMEOUT", 0.05)
    monkeypatch.setattr(demo, "CHILD_CLEANUP_TIMEOUT", 0.05)
    monkeypatch.setattr(demo, "READER_CLEANUP_TIMEOUT", 0.05)
    monkeypatch.setattr(sys, "argv", ["demo.py", "--duration", "3"])


def test_missing_peer_control_times_out_and_cleans_all_children(monkeypatch, capsys):
    children = [FakeChild(), FakeChild("Follower applied 0 commands\n"), FakeChild()]
    retained = children.copy()
    setup_fake_demo(monkeypatch, retained.copy())
    owned = retained.copy()
    monkeypatch.setattr(demo.subprocess, "Popen", lambda *_a, **_k: owned.pop(0))
    assert demo.main() == 1
    assert "startup timeout" in capsys.readouterr().err
    assert len(retained) == 3
    assert all(child.signals == 1 for child in retained)
    assert all(child.wait_calls >= 1 and child.returncode is not None for child in retained)


def test_unexpected_child_exit_fails_and_cleans_remaining_children(monkeypatch, capsys):
    children = [FakeChild(), FakeChild(), FakeChild(returncode=17)]
    setup_fake_demo(monkeypatch, children.copy())
    owned = children.copy()
    monkeypatch.setattr(demo.subprocess, "Popen", lambda *_a, **_k: owned.pop(0))
    assert demo.main() == 1
    assert "exited early" in capsys.readouterr().err
    assert all(child.returncode is not None for child in children)


def test_interrupt_cleanup_handles_exit_signal_race(monkeypatch):
    children = [FakeChild(), FakeChild(race=True), FakeChild()]
    setup_fake_demo(monkeypatch, children.copy())
    owned = children.copy()
    monkeypatch.setattr(demo.subprocess, "Popen", lambda *_a, **_k: owned.pop(0))

    def interrupt(*_args, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(demo.Queue, "get", interrupt)
    with pytest.raises(KeyboardInterrupt):
        demo.main()
    assert all(child.returncode is not None for child in children)
    assert all(child.wait_calls >= 1 for child in children)


def test_cleanup_kills_and_reaps_child_that_ignores_signal(monkeypatch):
    children = [FakeChild(), FakeChild(stubborn=True), FakeChild()]
    setup_fake_demo(monkeypatch, children.copy())
    owned = children.copy()
    monkeypatch.setattr(demo.subprocess, "Popen", lambda *_a, **_k: owned.pop(0))

    def interrupt(*_args, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(demo.Queue, "get", interrupt)
    with pytest.raises(KeyboardInterrupt):
        demo.main()
    assert len(children) == 3
    stubborn = children[1]
    assert stubborn.signals == 1
    assert stubborn.killed
    assert stubborn.wait_calls == 2
    assert stubborn.returncode == -9


def test_failure_never_prints_generated_token(monkeypatch, capsys):
    secret = "failure-token-that-must-not-leak"
    monkeypatch.setattr(demo.secrets, "token_urlsafe", lambda _: secret)
    children = [FakeChild(), FakeChild(), FakeChild(returncode=17)]
    setup_fake_demo(monkeypatch, children.copy())
    owned = children.copy()
    monkeypatch.setattr(demo.subprocess, "Popen", lambda *_a, **_k: owned.pop(0))
    assert demo.main() == 1
    captured = capsys.readouterr()
    assert secret not in captured.out
    assert secret not in captured.err


def test_real_server_readiness_can_be_delayed():
    # A real child waits before binding, so the readiness loop must tolerate a
    # live-but-not-ready process instead of starting endpoint runtime early.
    port = demo.free_port()
    code = (
        "import time; from aiohttp import web; import asyncio; "
        "time.sleep(.35); "
        "app=web.Application(); app.router.add_get('/health', lambda r: web.Response()); "
        "web.run_app(app, host='127.0.0.1', port=" + str(port) + ", print=None)"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", code],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        started = demo.time.monotonic()
        demo.wait_for_server(child, port, started + 3)
        assert demo.time.monotonic() - started >= 0.3
    finally:
        if child.poll() is None:
            child.terminate()
        child.wait(timeout=3)
