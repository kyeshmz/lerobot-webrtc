import argparse

import pytest

from lerobot_webrtc.cli import calibrate, main, positive, stun_url, turn_url


@pytest.mark.parametrize("value", ["nan", "inf", "0", "-1"])
def test_positive_rejects_invalid_values(value):
    with pytest.raises(argparse.ArgumentTypeError):
        positive(value)


def test_hardware_follower_requires_arm(monkeypatch, capsys):
    monkeypatch.setenv("LEROBOT_WEBRTC_TOKEN", "test-secret-at-least-16-characters")
    monkeypatch.setattr(
        "sys.argv", ["lerobot-webrtc", "follower", "--port", "/dev/fake", "--id", "follower"]
    )
    with pytest.raises(SystemExit) as exit_status:
        main()
    assert exit_status.value.code == 2
    assert "explicit --arm" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (["bench"], "explicit --execute"),
        (["bench", "--simulate", "--offset", "nan"], "at most 1 degree"),
        (["bench", "--simulate", "--offset", "2"], "at most 1 degree"),
        (["bench", "--simulate", "--duration", "60"], "duration 3–15"),
        (["bench", "--simulate", "--execute"], "not both"),
        (["leader", "--robot", "b601-rs", "--port", "can1", "--id", "arm"], "follower-only"),
        (
            ["follower", "--robot", "b601-rs", "--port", "can1", "--id", "arm", "--arm"],
            "--can-lock",
        ),
    ],
)
def test_b601_options_fail_before_endpoint_or_motor_access(monkeypatch, capsys, arguments, message):
    monkeypatch.setenv("LEROBOT_WEBRTC_TOKEN", "test-secret-at-least-16-characters")
    monkeypatch.setattr("sys.argv", ["lerobot-webrtc", *arguments])
    with pytest.raises(SystemExit) as status:
        main()
    assert status.value.code == 2
    assert message in capsys.readouterr().err


def test_bench_return_to_start_is_explicitly_optional():
    from lerobot_webrtc.cli import parser

    assert not parser().parse_args(["bench", "--simulate"]).require_return
    assert parser().parse_args(["bench", "--simulate", "--require-return"]).require_return


@pytest.mark.parametrize(
    ("argument", "value"),
    [
        ("--signaling", "ws://user:pass@localhost:8080"),
        ("--signaling", "ws://localhost:bad"),
        ("--signaling", "ws://localhost:70000"),
        ("--stun", "https://stun.example"),
        ("--stun", "stun:host:99999"),
        ("--turn", "https://turn.example"),
        ("--stun", "stuns:stun.example:3478"),
        ("--stun", "stun:user@host"),
        ("--turn", "turn:user@host"),
        ("--turn", "turn:host/path"),
        ("--turn", "turn:host:3478?transport=bogus"),
        ("--turn", "turns:host:5349?transport=udp"),
        ("--turn", "turn:host:0"),
    ],
)
def test_invalid_network_options_rejected_before_endpoint(monkeypatch, capsys, argument, value):
    monkeypatch.setenv("LEROBOT_WEBRTC_TOKEN", "test-secret-at-least-16-characters")
    monkeypatch.setattr("sys.argv", ["lerobot-webrtc", "leader", "--simulate", argument, value])
    with pytest.raises(SystemExit) as exit_status:
        main()
    assert exit_status.value.code == 2
    assert "error:" in capsys.readouterr().err


@pytest.mark.parametrize("port", ["0", "65536", "-1"])
def test_invalid_server_port_rejected(monkeypatch, capsys, port):
    monkeypatch.setenv("LEROBOT_WEBRTC_TOKEN", "test-secret-at-least-16-characters")
    monkeypatch.setattr("sys.argv", ["lerobot-webrtc", "serve", "--port", port])
    with pytest.raises(SystemExit) as exit_status:
        main()
    assert exit_status.value.code == 2
    assert "error:" in capsys.readouterr().err


@pytest.mark.parametrize(
    "uri",
    [
        "turn:relay:3478",
        "turn:relay?transport=udp",
        "turn:relay:3478?transport=tcp",
        "turns:relay:5349",
        "turns:relay:5349?transport=tcp",
    ],
)
def test_supported_turn_uris(uri):
    assert turn_url(uri) == uri


def test_stun_uri_support_matches_aiortc():
    assert stun_url("stun:relay.example:3478") == "stun:relay.example:3478"


def test_server_help_and_invalid_port_output_are_bounded(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["lerobot-webrtc", "serve", "--help"])
    with pytest.raises(SystemExit):
        main()
    assert len(capsys.readouterr().out) < 3000
    monkeypatch.setattr("sys.argv", ["lerobot-webrtc", "serve", "--port", "0"])
    with pytest.raises(SystemExit):
        main()
    assert len(capsys.readouterr().err) < 1000


def test_calibrate_disconnects_after_success(monkeypatch):
    calls = []

    class Device:
        def __init__(self, *args):
            calls.append(("init", args))

        def calibrate(self):
            calls.append(("calibrate",))

        def disconnect(self):
            calls.append(("disconnect",))

    monkeypatch.setattr("lerobot_webrtc.cli.LeRobotArm", Device)
    calibrate(
        argparse.Namespace(
            role="follower", robot="so101", port="fake", id="arm", max_relative_target=5
        )
    )
    assert calls == [
        ("init", ("follower", "so101", "fake", "arm", 5, "torque-off")),
        ("calibrate",),
        ("disconnect",),
    ]


def test_calibrate_disconnects_after_partial_setup_failure(monkeypatch):
    calls = []
    failure = RuntimeError("connect failed")

    class Device:
        def __init__(self, *args):
            pass

        def calibrate(self):
            calls.append("calibrate")
            raise failure

        def disconnect(self):
            calls.append("disconnect")

    monkeypatch.setattr("lerobot_webrtc.cli.LeRobotArm", Device)
    with pytest.raises(RuntimeError, match="connect failed"):
        calibrate(
            argparse.Namespace(
                role="leader", robot="so101", port="fake", id="arm", max_relative_target=5
            )
        )
    assert calls == ["calibrate", "disconnect"]


def test_calibrate_preserves_calibration_and_cleanup_errors(monkeypatch):
    class Device:
        def __init__(self, *args):
            pass

        def calibrate(self):
            raise RuntimeError("calibration failed")

        def disconnect(self):
            raise RuntimeError("disconnect failed")

    monkeypatch.setattr("lerobot_webrtc.cli.LeRobotArm", Device)
    with pytest.raises(BaseExceptionGroup, match="both failed") as caught:
        calibrate(
            argparse.Namespace(
                role="leader", robot="so101", port="fake", id="arm", max_relative_target=5
            )
        )
    assert [str(error) for error in caught.value.exceptions] == [
        "calibration failed",
        "disconnect failed",
    ]
