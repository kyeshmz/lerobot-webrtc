import asyncio
import json

import pytest
import pytest_asyncio
from aiohttp import web

from examples.cloudflare import with_turn


def ice_servers():
    return {
        "iceServers": [
            {"urls": ["stun:stun.cloudflare.com:3478"]},
            {
                "urls": [
                    "turn:turn.cloudflare.com:53?transport=udp",
                    "turn:turn.cloudflare.com:443?transport=udp",
                    "turn:turn.cloudflare.com:3478?transport=udp",
                    "turn:turn.cloudflare.com:3478?transport=tcp",
                    "turns:turn.cloudflare.com:5349?transport=tcp",
                    "turns:turn.cloudflare.com:443?transport=tcp",
                ],
                "username": "temporary-user",
                "credential": "temporary-password",
            },
        ]
    }


@pytest_asyncio.fixture
async def cloudflare_api(unused_tcp_port, monkeypatch):
    state = {"status": 201, "payload": ice_servers(), "calls": [], "leaks": 0}

    async def credentials(request):
        state["calls"].append(
            {
                "path": request.path,
                "auth": request.headers.get("Authorization"),
                "body": await request.json(),
            }
        )
        if state["status"] == 302:
            return web.Response(status=302, headers={"Location": "/leak"})
        if state.get("chunked"):
            response = web.StreamResponse(status=201, headers={"Content-Type": "application/json"})
            await response.prepare(request)
            body = json.dumps(state["payload"]).encode()
            for index in range(0, len(body), 23):
                await response.write(body[index : index + 23])
                await asyncio.sleep(0.001)
            await response.write_eof()
            return response
        return web.json_response(state["payload"], status=state["status"])

    async def leak(request):
        state["leaks"] += 1
        return web.json_response(ice_servers())

    app = web.Application()
    app.router.add_post("/v1/turn/keys/test-key/credentials/generate-ice-servers", credentials)
    app.router.add_route("*", "/leak", leak)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", unused_tcp_port).start()
    monkeypatch.setattr(with_turn, "API_BASE", f"http://127.0.0.1:{unused_tcp_port}")
    try:
        yield state
    finally:
        await runner.cleanup()


async def test_current_cloudflare_endpoint_and_array_response(cloudflare_api):
    response = await with_turn.request_ice_servers("test-key", "test-server-api-token", 600)
    assert response == ice_servers()
    assert cloudflare_api["calls"] == [
        {
            "path": "/v1/turn/keys/test-key/credentials/generate-ice-servers",
            "auth": "Bearer test-server-api-token",
            "body": {"ttl": 600},
        }
    ]


async def test_chunked_provider_json_is_read_completely(cloudflare_api):
    cloudflare_api["chunked"] = True
    assert (
        await with_turn.request_ice_servers("test-key", "test-server-api-token", 600)
        == ice_servers()
    )


@pytest.mark.parametrize("status", [302, 403, 500])
async def test_provider_errors_are_redacted_and_redirects_are_not_followed(cloudflare_api, status):
    cloudflare_api["status"] = status
    cloudflare_api["payload"] = {"error": "provider-secret-that-must-not-be-echoed"}
    with pytest.raises(with_turn.TurnSetupError) as error:
        await with_turn.request_ice_servers("test-key", "test-server-api-token", 600)
    assert str(status) in str(error.value)
    assert "provider-secret" not in str(error.value)
    assert "test-server-api-token" not in str(error.value)
    assert cloudflare_api["leaks"] == 0


@pytest.mark.parametrize(
    ("transport", "url"),
    [
        ("udp", "turn:turn.cloudflare.com:3478?transport=udp"),
        ("tcp", "turn:turn.cloudflare.com:3478?transport=tcp"),
        ("tls", "turns:turn.cloudflare.com:443?transport=tcp"),
    ],
)
def test_selects_one_supported_uri_and_skips_port_53(transport, url):
    result = with_turn.select_credentials(ice_servers(), transport)
    assert result.url == url
    assert result.stun == "stun:stun.cloudflare.com:3478"
    assert result.username == "temporary-user"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"iceServers": {}},
        {"iceServers": [None]},
        {"iceServers": [{"urls": ["turn:turn.cloudflare.com:3478?transport=udp"]}]},
        {
            "iceServers": [
                {
                    "urls": ["turn:turn.cloudflare.com:53?transport=udp"],
                    "username": "u",
                    "credential": "p",
                }
            ]
        },
    ],
)
def test_invalid_or_legacy_ice_response_fails_closed(payload):
    with pytest.raises(with_turn.TurnSetupError):
        with_turn.select_credentials(payload, "udp")


def test_endpoint_gets_only_temporary_credentials_not_provider_api_key(monkeypatch):
    calls = []
    monkeypatch.setattr(with_turn.os, "execve", lambda *args: calls.append(args))
    source = {
        "TURN_KEY_ID": "test-key",
        "TURN_KEY_API_TOKEN": "server-only-secret",
        "LEROBOT_WEBRTC_TOKEN": "shared-signaling-secret",
        "PATH": "/test/path",
    }
    credentials = with_turn.select_credentials(ice_servers(), "tls")
    with_turn.launch_credentials(
        ["follower", "--simulate", "--duration", "10"], credentials, source
    )
    executable, argv, env = calls[0]
    assert argv[:3] == [executable, "-m", "lerobot_webrtc"]
    assert "--turn" in argv
    assert "server-only-secret" not in str(argv)
    assert "temporary-password" not in str(argv)
    assert "TURN_KEY_API_TOKEN" not in env and "TURN_KEY_ID" not in env
    assert env["LEROBOT_TURN_USERNAME"] == "temporary-user"
    assert env["LEROBOT_TURN_PASSWORD"] == "temporary-password"
    assert env["LEROBOT_WEBRTC_TOKEN"] == "shared-signaling-secret"
    assert source["TURN_KEY_API_TOKEN"] == "server-only-secret"


@pytest.mark.parametrize(
    "arguments",
    [
        ["--", "serve"],
        ["--", "leader", "--simulate"],
        ["--ttl", "120", "--", "leader", "--simulate", "--duration", "60"],
        ["--", "leader", "--simulate", "--duration", "10", "--turn", "turn:example.com:3478"],
    ],
)
def test_invalid_launch_never_requests_provider_credentials(monkeypatch, arguments):
    async def forbidden(*args):
        pytest.fail("Invalid example commands must fail before a provider call")

    monkeypatch.setattr(with_turn, "request_ice_servers", forbidden)
    monkeypatch.setenv("LEROBOT_WEBRTC_TOKEN", "shared-test-token-at-least-16")
    with pytest.raises(SystemExit) as status:
        with_turn.main(arguments)
    assert status.value.code == 2


def test_successful_launcher_never_prints_secrets(monkeypatch, capsys):
    async def credentials(*args):
        return ice_servers()

    monkeypatch.setattr(with_turn, "request_ice_servers", credentials)
    monkeypatch.setattr(with_turn.os, "execve", lambda *args: None)
    monkeypatch.setenv("LEROBOT_WEBRTC_TOKEN", "shared-test-token-at-least-16")
    monkeypatch.setenv("TURN_KEY_ID", "test-key")
    monkeypatch.setenv("TURN_KEY_API_TOKEN", "server-only-secret")
    assert with_turn.main(["--", "leader", "--simulate", "--duration", "10"]) == 0
    output = capsys.readouterr()
    assert not output.out and not output.err


@pytest.mark.parametrize("ttl", [0, 119, 3601, True])
async def test_ttl_validation_happens_before_network(ttl):
    with pytest.raises(with_turn.TurnSetupError, match="TTL"):
        await with_turn.request_ice_servers("test-key", "test-server-api-token", ttl)
